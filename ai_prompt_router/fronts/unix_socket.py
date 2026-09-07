"""Unix-socket front — the router stands in for the native host.

`ai-prompt-cli/ai-prompt-native.py` does not speak HTTP. It opens
`/tmp/ai-prompt-native.sock` and exchanges 4-byte little-endian length-prefixed
JSON with `ai_prompt_api/native_host.py:handle_cli_client()`. This front reproduces
that server side exactly, on a path of its own (`unix_socket.path`, default
`/tmp/ai-prompt-router.sock` — the native host already owns the other one, and both
may be running on the same box), so the CLI can be pointed at a whole fleet of
browsers with `--socket` instead of at a single browser.

Two message types cross this wire:

  * `{"type":"status", …}`  → `{"type":"status", "request_id":…, "connected":…}`,
    where `connected` is `router.any_healthy()` — for the CLI this is the same
    question as "is a browser reachable?", it just now means "any of them" (§5.5).
  * anything else is a prompt, forwarded to `router.forward_prompt()` and answered
    with the extension's `response` frame (the native host returns the extension's
    reply verbatim, so the CLI already parses exactly that shape).

Unlike the WebSocket front, the prompt frame here *does* carry a `timeout` (the
CLI's `--timeout`), and the plan says to honour it.

Auth is the file mode, exactly as today: the socket is created 0600 so only the
user running the router can talk to it.
"""

import asyncio
import contextlib
import json
import logging
import os
import struct
from typing import Any, Dict, Optional

from ai_prompt_api.models import PromptRequest

logger = logging.getLogger(__name__)

# Same clamp as the WebSocket front: PromptRequest validates 0 < timeout <= 600,
# while the CLI's --timeout is an unbounded float.
MAX_REQUEST_TIMEOUT = 600.0
DEFAULT_REQUEST_TIMEOUT = 120.0  # what native_host.handle_cli_client() falls back to

# A single frame is length-prefixed with a 32-bit count, so a corrupt or hostile
# prefix could otherwise ask us to buffer 4 GiB. Attachments are base64 in JSON,
# hence a generous but finite ceiling.
MAX_FRAME_BYTES = 64 * 1024 * 1024


def _dump_images(images: Any) -> list:
    if not images:
        return []
    out = []
    for image in images:
        dump = getattr(image, "model_dump", None)
        out.append(dump() if callable(dump) else image)
    return out


def build_response_message(request_id: Any, response: Any) -> Dict[str, Any]:
    """The extension's `response` frame (see background.js:handlePrompt()).

    Deliberately duplicated from `ws_client.build_response_message` rather than
    imported: this front must keep working on a box where the `websockets` package
    is not installed, and importing the WebSocket module would drag it in.
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


class UnixSocketFront:
    """Length-prefixed JSON server on a 0600 unix socket."""

    def __init__(self, path: str, router: Any) -> None:
        self.path = path
        self.router = router
        self._server: Optional[asyncio.AbstractServer] = None
        # Live connection count (what `GET /upstreams` shows) and the per-request
        # tasks we may have to cancel on shutdown.
        self._connections = 0
        self._inflight: set = set()

    # -- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        if self._server is not None:
            return

        parent = os.path.dirname(os.path.abspath(self.path))
        if parent:
            os.makedirs(parent, exist_ok=True)

        # A socket file left behind by a crashed router would make bind() fail with
        # EADDRINUSE forever, so remove it first — the same thing the native host
        # does. (Note the trade-off it accepts: if another router really is alive on
        # this path we steal it. One socket path per router process.)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.path)

        # bind() applies the umask, so create the socket already private instead of
        # leaving a window between bind and chmod where it is world-writable. The
        # chmod afterwards is belt-and-braces (and matches the native host).
        previous_umask = os.umask(0o177)
        try:
            self._server = await asyncio.start_unix_server(self._handle_client, self.path)
        finally:
            os.umask(previous_umask)
        os.chmod(self.path, 0o600)
        logger.info("unix socket front listening at %s", self.path)

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
            with contextlib.suppress(Exception):
                await server.wait_closed()

        for task in list(self._inflight):
            task.cancel()
        if self._inflight:
            await asyncio.gather(*self._inflight, return_exceptions=True)
            self._inflight.clear()

        # Leave no stale socket file behind for the next start() (or for an
        # operator wondering whether the router is running).
        with contextlib.suppress(FileNotFoundError, OSError):
            os.unlink(self.path)
        self._connections = 0

    def snapshot(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "listening": self._server is not None,
            "connections": self._connections,
        }

    # -- connection handling --------------------------------------------------

    async def _handle_client(self, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter) -> None:
        """One connected CLI. Every failure here is contained to this client."""
        self._connections += 1
        send_lock = asyncio.Lock()
        client_tasks: set = set()
        try:
            while True:
                length_bytes = await reader.readexactly(4)
                (length,) = struct.unpack("<I", length_bytes)
                if length > MAX_FRAME_BYTES:
                    raise ValueError(f"frame of {length} bytes exceeds the limit")
                raw = await reader.readexactly(length)

                try:
                    message = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError) as exc:
                    # Framing is still intact, so answer and keep the connection.
                    await self._write(writer, send_lock,
                                      _error_message(None, f"Invalid JSON: {exc}"))
                    continue

                # Dispatch instead of awaiting: a prompt can take minutes, and the
                # reply carries its own request_id, so a client that pipelines gets
                # its answers matched up regardless of completion order. (Today's
                # CLI opens one connection per request, so this only ever helps.)
                task = asyncio.create_task(
                    self._handle_message(writer, send_lock, message))
                client_tasks.add(task)
                self._inflight.add(task)
                task.add_done_callback(client_tasks.discard)
                task.add_done_callback(self._inflight.discard)
        except asyncio.IncompleteReadError:
            pass  # normal: the CLI closes after reading its response
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad client must not stop the server
            logger.warning("unix socket front: client error: %s", exc)
        finally:
            self._connections -= 1
            # Requests still running for this client have nowhere to reply to.
            for task in list(client_tasks):
                task.cancel()
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _handle_message(self, writer: asyncio.StreamWriter,
                              send_lock: asyncio.Lock,
                              message: Dict[str, Any]) -> None:
        request_id = message.get("request_id") if isinstance(message, dict) else None
        try:
            if isinstance(message, dict) and message.get("type") == "status":
                # `ai-prompt-native.py --status` prints this. For the router,
                # "connected" means at least one upstream is healthy (§5.5).
                payload = {
                    "type": "status",
                    "request_id": request_id,
                    "connected": bool(self.router.any_healthy()),
                }
            else:
                payload = await self._forward(message, request_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("unix socket front: request %s failed", request_id)
            payload = _error_message(request_id, f"{type(exc).__name__}: {exc}")

        await self._write(writer, send_lock, payload)

    async def _forward(self, message: Any, request_id: Any) -> Dict[str, Any]:
        if not isinstance(message, dict):
            return _error_message(request_id, "Invalid prompt message: expected an object")
        try:
            request = self._build_request(message)
        except Exception as exc:  # noqa: BLE001 - e.g. empty text, unknown provider
            return _error_message(request_id, f"Invalid prompt message: {exc}")

        # Entry point, so hops=0/via=None: the hop guard is an HTTP-only concern
        # (§5.1); nothing can chain in through a 0600 socket on this box.
        result = await self.router.forward_prompt(request, hops=0, via=None)
        return build_response_message(request_id, result.response)

    def _build_request(self, message: Dict[str, Any]) -> PromptRequest:
        """Map the CLI's prompt frame onto the shared model.

        Unlike the WebSocket front, `timeout` is taken from the message when the
        client sent one — that is the CLI's `--timeout`, and the caller stops
        waiting at exactly that point, so the router must not outlive it.
        """
        timeout = message.get("timeout")
        try:
            timeout = float(timeout) if timeout is not None else DEFAULT_REQUEST_TIMEOUT
        except (TypeError, ValueError):
            timeout = DEFAULT_REQUEST_TIMEOUT

        return PromptRequest(
            text=message.get("text") or "",
            provider=message.get("provider") or None,
            session_id=message.get("session_id") or None,
            timeout=max(min(timeout, MAX_REQUEST_TIMEOUT), 0.001),
            ephemeral=bool(message.get("ephemeral", False)),
            attachments=message.get("attachments") or None,
        )

    async def _write(self, writer: asyncio.StreamWriter, send_lock: asyncio.Lock,
                     payload: Dict[str, Any]) -> None:
        data = json.dumps(payload).encode("utf-8")
        try:
            # Concurrent handlers share one stream; without the lock two responses
            # could interleave and desynchronise the length framing for good.
            async with send_lock:
                writer.write(struct.pack("<I", len(data)))
                writer.write(data)
                await writer.drain()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - client hung up mid-request
            logger.warning("unix socket front: dropping response %s (%s)",
                           payload.get("request_id"), exc)
