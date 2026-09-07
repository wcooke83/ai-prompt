"""WebSocket front — the router dials OUT and impersonates the browser extension.

Every other piece of this system is a server waiting to be called; this one is the
odd one out, and deliberately so. `ai-prompt-cli/ai-prompt.py` and the standalone
bridge (`ai_prompt_api/main.py`) do not connect to anything: they *listen* on a
WebSocket port and wait for a browser extension to dial in (see
`ChatGPTCLI.handle_connection`). A browser extension cannot listen on a TCP port,
so the direction of the socket is fixed by the extension's limitation, not by
preference.

To let those two callers use the router without a single line of change, the
router takes the extension's place: for each configured `ws_targets[]` URL it
keeps a client connection alive, waits for `{type:"prompt", …}` frames, forwards
them through `router.forward_prompt()`, and answers with the byte-for-byte shape
`background.js:handlePrompt()` sends back. From the CLI's point of view a browser
connected; it never learns the "extension" is a fleet of browsers behind a router.

Two behaviours are copied verbatim from `ai-prompt-extension/background.js`:

  * the reconnect cadence of `scheduleReconnect()` — constant 500 ms in fast mode,
    otherwise exponential 1 s → 30 s capped, with the attempt counter reset by a
    successful open. The CLI is short-lived (it starts a server, sends one prompt,
    exits), so a target vanishing and reappearing is the normal case, not an error
    — which is why the extension defaults `fastReconnect` to true and so do we:
    with the backoff, the CLI invoked ten seconds later would sit there waiting for
    an "extension" that is asleep for another twenty.
  * the `response` frame of `handlePrompt()`, down to the falsy-to-null coercions
    and the fact that `error` is present only on failure.

There is no handshake and no auth on this front: the CLI's server accepts one
client and starts talking. That is the same trust model as today (localhost), and
it is safe in the same way — we only ever dial targets that are in our own config.
"""

import asyncio
import inspect
import json
import logging
from typing import Any, Dict, Iterable, List, Optional

import websockets

from ai_prompt_api.models import PromptRequest

logger = logging.getLogger(__name__)

# --- reconnect cadence, mirroring background.js:scheduleReconnect() -----------
# Module-level so tests (and a future config knob) can shorten them without
# reaching into instances.
FAST_RECONNECT_SECONDS = 0.5
RECONNECT_BASE_SECONDS = 1.0
RECONNECT_MAX_SECONDS = 30.0

# A dial that hangs (host up, nothing accepting) must not wedge a target forever.
OPEN_TIMEOUT_SECONDS = 10.0

# `PromptRequest.timeout` is validated as 0 < t <= 600. The router's default is a
# plain float from config, so clamp rather than let a misconfiguration turn every
# prompt into a 422-shaped ValidationError.
MAX_REQUEST_TIMEOUT = 600.0


def _connect_extra_kwargs() -> Dict[str, Any]:
    """Kwargs for `websockets.connect` that only exist on newer releases.

    websockets >= 13 honours HTTP_PROXY/ALL_PROXY for ws:// URLs by default, which
    would silently route a connection to a *localhost* bridge through a corporate
    proxy. Our targets are always ones we configured ourselves, so opt out — but
    only when the installed version knows the argument, since older releases pass
    unknown kwargs down to `loop.create_connection()` and raise there.
    """
    try:
        params = inspect.signature(websockets.connect).parameters
    except (TypeError, ValueError):  # pragma: no cover - exotic websockets build
        return {}
    return {"proxy": None} if "proxy" in params else {}


def _coerce_url(target: Any) -> str:
    """Accept either `"ws://host:port"` or the config's `{"url": …}` object.

    `router.json` stores `ws_targets` as objects (§4 of the plan) while the front's
    contract takes plain strings; tolerating both means neither side has to care
    which one the caller happened to have.
    """
    if isinstance(target, str):
        return target
    if isinstance(target, dict) and target.get("url"):
        return str(target["url"])
    raise ValueError(f"Unsupported ws target: {target!r}")


def _dump_images(images: Any) -> List[Any]:
    """Serialise `PromptResponse.images` for the wire.

    Pydantic gives us `ImageData` objects, but a stub/legacy upstream may hand back
    plain dicts; both are valid JSON payloads once dumped.
    """
    if not images:
        return []
    out = []
    for image in images:
        dump = getattr(image, "model_dump", None)
        out.append(dump() if callable(dump) else image)
    return out


def build_response_message(request_id: Any, response: Any) -> Dict[str, Any]:
    """Build the extension's `response` frame from a `PromptResponse`.

    Mirrors `background.js:handlePrompt()` exactly, including its `|| null` /
    `|| []` coercions (a falsy token count becomes null there, so it does here) and
    the rule that `error` is attached only when `success` is false. Callers on this
    wire — `ChatGPTCLI.send_prompt` and `ai-prompt-native.py` — read these keys
    positionally-by-name, so drifting from the shape breaks them silently.
    """
    message = {
        "type": "response",
        "request_id": request_id,
        "success": bool(getattr(response, "success", False)),
        "text": getattr(response, "text", None) or None,
        "images": _dump_images(getattr(response, "images", None)),
        "provider": getattr(response, "provider", None) or None,
        "model": getattr(response, "model", None) or None,
        "input_tokens": getattr(response, "input_tokens", None) or None,
        "output_tokens": getattr(response, "output_tokens", None) or None,
        "session_id": getattr(response, "session_id", None) or None,
    }
    if not message["success"]:
        message["error"] = getattr(response, "error", None) or "Unknown error"
    return message


def _error_message(request_id: Any, error: str) -> Dict[str, Any]:
    """A failure frame for problems that never reached the router at all."""
    return {
        "type": "response",
        "request_id": request_id,
        "success": False,
        "text": None,
        "images": [],
        "provider": None,
        "model": None,
        "input_tokens": None,
        "output_tokens": None,
        "session_id": None,
        "error": error,
    }


class _WsTarget:
    """One dial-out connection, retried forever until the front is stopped."""

    def __init__(self, url: str, router: Any, default_timeout: float,
                 fast_reconnect: bool = True) -> None:
        self.url = url
        self.router = router
        self.default_timeout = default_timeout
        self.fast_reconnect = fast_reconnect

        self.connected = False
        self.last_error: Optional[str] = None
        # Consecutive failed dials since the last successful open. Drives the
        # backoff (and is what /upstreams shows an operator).
        self.attempts = 0

        self._task: Optional[asyncio.Task] = None
        self._inflight: set = set()
        # websockets does not support two coroutines writing to one connection at
        # the same time, and we deliberately answer prompts concurrently.
        self._send_lock = asyncio.Lock()

    # -- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name=f"ws-front:{self.url}")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - shutdown must not raise
                logger.debug("ws front %s ended with an error", self.url, exc_info=True)

        # In-flight prompts outlive the read loop by design (they are separate
        # tasks); on shutdown there is nowhere to send their answers, so drop them.
        for pending in list(self._inflight):
            pending.cancel()
        if self._inflight:
            await asyncio.gather(*self._inflight, return_exceptions=True)
            self._inflight.clear()
        self.connected = False

    def snapshot(self) -> Dict[str, Any]:
        return {
            "url": self.url,
            "connected": self.connected,
            "last_error": self.last_error,
            "attempts": self.attempts,
        }

    # -- connection loop ------------------------------------------------------

    def _next_delay(self) -> float:
        """Delay before the next dial — `scheduleReconnect()` in Python.

        Fast mode — the extension's default, and ours — polls at a flat 500 ms
        because a target is normally expected straight back (a CLI invoked in a
        loop). With `fast_reconnect=False` it is 1 s, 2 s, 4 s … capped at 30 s.
        `self.attempts` has already been incremented for this failure, hence the
        `attempts - 1` exponent — it keeps the first retry at 1 s like the
        extension, which increments *after* computing the delay.
        """
        if self.fast_reconnect:
            return FAST_RECONNECT_SECONDS
        return min(RECONNECT_BASE_SECONDS * (2 ** max(self.attempts - 1, 0)),
                   RECONNECT_MAX_SECONDS)

    async def _run(self) -> None:
        extra = _connect_extra_kwargs()
        while True:
            try:
                # max_size=None: a `prompt` frame can carry base64 attachments, and
                # the default 1 MiB cap would drop the connection mid-message.
                async with websockets.connect(
                    self.url,
                    open_timeout=OPEN_TIMEOUT_SECONDS,
                    max_size=None,
                    **extra,
                ) as ws:
                    self.connected = True
                    self.last_error = None
                    self.attempts = 0  # == `reconnectAttempts = 0` in ws.onopen
                    logger.info("ws front connected to %s", self.url)
                    await self._read_loop(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - any failure means "retry"
                self.attempts += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.debug("ws front %s: %s", self.url, self.last_error)
            else:
                # Clean close by the peer (the CLI exits after its one prompt).
                self.attempts += 1
                self.last_error = None
            finally:
                self.connected = False

            await asyncio.sleep(self._next_delay())

    async def _read_loop(self, ws: Any) -> None:
        async for raw in ws:
            try:
                message = json.loads(raw)
            except (TypeError, ValueError):
                logger.warning("ws front %s: ignoring non-JSON frame", self.url)
                continue
            if not isinstance(message, dict) or message.get("type") != "prompt":
                # The extension ignores everything that is not a prompt; so do we.
                continue

            # Never await the router here. A prompt takes up to `timeout` seconds
            # and the bridges accept concurrent prompts (each opens its own tab), so
            # blocking the read loop would serialise a queue that has no reason to
            # exist and would also stall the connection's control frames.
            task = asyncio.create_task(self._handle_prompt(ws, message))
            self._inflight.add(task)
            task.add_done_callback(self._inflight.discard)

    # -- prompt handling ------------------------------------------------------

    def _build_request(self, message: Dict[str, Any]) -> PromptRequest:
        """Map an extension `prompt` frame onto the shared `PromptRequest` model.

        The WebSocket `prompt` frame has no `timeout` field (`ChatGPTCLI.send_prompt`
        keeps its deadline client-side and simply stops waiting), so this front
        always applies the router's default — §5.1 of the plan.
        """
        return PromptRequest(
            text=message.get("text") or "",
            provider=message.get("provider") or None,
            session_id=message.get("session_id") or None,
            timeout=max(min(self.default_timeout, MAX_REQUEST_TIMEOUT), 0.001),
            ephemeral=bool(message.get("ephemeral", False)),
            attachments=message.get("attachments") or None,
        )

    async def _handle_prompt(self, ws: Any, message: Dict[str, Any]) -> None:
        request_id = message.get("request_id")
        try:
            request = self._build_request(message)
        except Exception as exc:  # noqa: BLE001 - malformed frame from the peer
            payload = _error_message(request_id, f"Invalid prompt message: {exc}")
        else:
            try:
                # hops=0/via=None: this front is an entry point. Hop headers are an
                # HTTP-only concern (§5.1) because nothing can chain into a socket
                # we dialled ourselves.
                result = await self.router.forward_prompt(request, hops=0, via=None)
                payload = build_response_message(request_id, result.response)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - never lose a caller's request
                logger.exception("ws front %s: forward failed", self.url)
                payload = _error_message(request_id, f"{type(exc).__name__}: {exc}")

        await self._send(ws, payload)

    async def _send(self, ws: Any, payload: Dict[str, Any]) -> None:
        data = json.dumps(payload)
        try:
            async with self._send_lock:
                await ws.send(data)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            # The peer went away while the prompt was running. The extension logs
            # this loudly rather than silently discarding a completed capture, and
            # so do we — the work was done, it just has nowhere to go.
            logger.warning("ws front %s: dropping response %s (%s)",
                           self.url, payload.get("request_id"), exc)


class WebSocketFront:
    """Keeps one dial-out extension impersonation alive per configured target."""

    def __init__(self, targets: Iterable[Any], router: Any,
                 default_timeout: float = 120.0, *,
                 fast_reconnect: bool = True) -> None:
        self.router = router
        self.default_timeout = default_timeout
        self._targets = [
            _WsTarget(_coerce_url(target), router, default_timeout, fast_reconnect)
            for target in (targets or [])
        ]

    async def start(self) -> None:
        for target in self._targets:
            await target.start()
        if self._targets:
            logger.info("ws front dialling %d target(s)", len(self._targets))

    async def stop(self) -> None:
        await asyncio.gather(*(target.stop() for target in self._targets),
                             return_exceptions=True)

    def snapshot(self) -> List[Dict[str, Any]]:
        """Per-target connection state for `GET /upstreams` (§5.5)."""
        return [target.snapshot() for target in self._targets]
