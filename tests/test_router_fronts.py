"""Tests for the router's CLI fronts (plan §7 "Fronts").

Both fronts impersonate something that already exists, so the tests drive them
with the *real* peer protocol rather than a paraphrase of it:

  * the WebSocket front is dialled into by a server built to the same shape as
    `ai-prompt-cli/ai-prompt.py`'s (`ChatGPTCLI.handle_connection` /
    `send_prompt`): accept one client, push `prompt`, match `response` by
    `request_id`;
  * the unix-socket front is driven through `ai-prompt-cli/ai-prompt-native.py`
    itself, loaded from source, so the 4-byte length-prefixed framing under test is
    literally the framing shipped to users.

The router core is owned by another module and may not exist yet, so everything
here runs against `StubRouter`, which implements exactly the two methods the
fronts are allowed to call. One test reaches for the real `Router` and skips
cleanly while it is still being written.

Tests are plain sync functions driving `asyncio.run()` on purpose: that keeps the
file independent of whatever `asyncio_mode` the shared conftest ends up setting.
"""

import asyncio
import contextlib
import importlib.util
import inspect
import json
import os
import shutil
import socket
import stat
import struct
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional

import pytest
import websockets

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ai_prompt_api.models import ImageData, PromptResponse  # noqa: E402
from ai_prompt_router.fronts.unix_socket import UnixSocketFront  # noqa: E402
from ai_prompt_router.fronts.ws_client import WebSocketFront  # noqa: E402
from ai_prompt_router.fronts import ws_client  # noqa: E402


# ============ helpers =========================================================

def run(coro):
    """Every test is a coroutine; give each one its own event loop."""
    return asyncio.run(coro)


def _load_native_cli():
    """Import ai-prompt-native.py by path (its name is not a valid identifier)."""
    path = REPO_ROOT / "ai-prompt-cli" / "ai-prompt-native.py"
    spec = importlib.util.spec_from_file_location("ai_prompt_native_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


native_cli = _load_native_cli()


def free_port() -> int:
    """Reserve-and-release a port so the restart test can rebind the same one."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def wait_for(predicate, timeout=15.0, interval=0.05):
    """Poll until `predicate()` is truthy; return whether it ever was."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


@dataclass
class StubForwardResult:
    """Stand-in for the core's ForwardResult dataclass."""
    response: PromptResponse
    upstream: Optional[str] = None
    attempts: List[str] = field(default_factory=list)


class StubRouter:
    """The whole surface a front is allowed to use: forward_prompt + any_healthy."""

    def __init__(self, response: Optional[PromptResponse] = None, healthy: bool = True,
                 gate: Optional[asyncio.Event] = None, raises: Optional[Exception] = None):
        self.response = response or PromptResponse(
            success=True, text="hi", session_id="sess_1", provider="chatgpt")
        self.healthy = healthy
        self.gate = gate
        self.raises = raises

        self.requests: List[Any] = []
        self.calls: List[dict] = []       # the hops/via each front passed
        self.started = 0
        self.in_flight = 0
        self.max_in_flight = 0

    async def forward_prompt(self, request, *, hops: int = 0, via=None):
        self.requests.append(request)
        self.calls.append({"hops": hops, "via": via})
        self.started += 1
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.gate is not None:
                await self.gate.wait()
            if self.raises is not None:
                raise self.raises
            return StubForwardResult(response=self.response, upstream="stub")
        finally:
            self.in_flight -= 1

    def any_healthy(self) -> bool:
        return self.healthy


class FakeCliServer:
    """A WebSocket *server* shaped like `ai-prompt.py`'s.

    No handshake, one client at a time, `prompt` out and `response` back matched on
    `request_id` — the exact contract the router's WebSocket front has to satisfy.
    """

    def __init__(self, port: int):
        self.port = port
        self.client = None
        self.connected = asyncio.Event()
        self._server = None
        self._pending = {}

    async def start(self):
        self._server = await websockets.serve(self._handle, "127.0.0.1", self.port)

    async def _handle(self, ws):
        self.client = ws
        self.connected.set()
        try:
            async for raw in ws:
                message = json.loads(raw)
                if message.get("type") == "response":
                    future = self._pending.pop(message.get("request_id"), None)
                    if future is not None and not future.done():
                        future.set_result(message)
        except Exception:  # noqa: BLE001 - mirrors the CLI's ConnectionClosed catch
            pass
        finally:
            self.client = None
            self.connected.clear()

    async def wait_connected(self, timeout=15.0):
        await asyncio.wait_for(self.connected.wait(), timeout)

    async def send_prompt(self, text, **extra):
        """Fire a prompt; return the future its `response` will land in."""
        request_id = str(uuid.uuid4())
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        message = {"type": "prompt", "request_id": request_id, "text": text}
        message.update(extra)
        await self.client.send(json.dumps(message))
        return request_id, future

    async def prompt(self, text, timeout=15.0, **extra):
        _, future = await self.send_prompt(text, **extra)
        return await asyncio.wait_for(future, timeout)

    async def stop(self):
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None


@contextlib.asynccontextmanager
async def ws_front(server_port, router, **kwargs):
    front = WebSocketFront([f"ws://127.0.0.1:{server_port}"], router, **kwargs)
    await front.start()
    try:
        yield front
    finally:
        await front.stop()


@pytest.fixture
def sock_path():
    """A *short* socket path.

    AF_UNIX caps the whole path at ~104 bytes on macOS and pytest's `tmp_path` is
    already longer than that, so the usual fixture cannot be used here.
    """
    directory = tempfile.mkdtemp(prefix="aiprt-", dir="/tmp")
    try:
        yield Path(directory) / "router.sock"
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@contextlib.asynccontextmanager
async def unix_front(path, router):
    front = UnixSocketFront(str(path), router)
    await front.start()
    try:
        yield front
    finally:
        await front.stop()


async def raw_unix_request(path, message, timeout=15.0):
    """Length-prefixed round trip without going through the CLI module."""
    reader, writer = await asyncio.open_unix_connection(str(path))
    try:
        encoded = json.dumps(message).encode("utf-8")
        writer.write(struct.pack("<I", len(encoded)))
        writer.write(encoded)
        await writer.drain()
        header = await asyncio.wait_for(reader.readexactly(4), timeout)
        (length,) = struct.unpack("<I", header)
        body = await asyncio.wait_for(reader.readexactly(length), timeout)
        return json.loads(body.decode("utf-8"))
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


# ============ WebSocket front =================================================

def test_ws_front_prompt_round_trip():
    """A CLI-shaped server gets back exactly the extension's `response` frame."""
    router = StubRouter(PromptResponse(
        success=True, text="the answer", session_id="sess_abc", provider="grok",
        model="grok-4", input_tokens=11, output_tokens=22,
        images=[ImageData(src="data:image/png;base64,AA", alt="chart")]))

    async def scenario():
        server = FakeCliServer(free_port())
        await server.start()
        try:
            async with ws_front(server.port, router):
                await server.wait_connected()
                return await server.prompt("what is 2+2?", provider="grok",
                                           session_id="sess_abc")
        finally:
            await server.stop()

    response = run(scenario())

    assert response["type"] == "response"
    assert response["success"] is True
    assert response["text"] == "the answer"
    assert response["session_id"] == "sess_abc"
    assert response["provider"] == "grok"
    assert response["model"] == "grok-4"
    assert response["input_tokens"] == 11
    assert response["output_tokens"] == 22
    assert response["images"] == [{"src": "data:image/png;base64,AA", "alt": "chart"}]
    # background.js attaches `error` only on failure; the CLI keys off `success`
    # but other consumers sniff for `error`, so parity matters.
    assert "error" not in response

    request = router.requests[-1]
    assert request.text == "what is 2+2?"
    assert request.session_id == "sess_abc"
    assert request.provider.value == "grok"
    # The WS `prompt` frame has no timeout field -> the router default applies.
    assert request.timeout == 120.0
    # Entry point: hop headers are HTTP-only (plan §5.1).
    assert router.calls[-1] == {"hops": 0, "via": None}


def test_ws_front_failure_carries_an_error_field():
    router = StubRouter(PromptResponse(success=False, error="Timeout after 120s"))

    async def scenario():
        server = FakeCliServer(free_port())
        await server.start()
        try:
            async with ws_front(server.port, router):
                await server.wait_connected()
                return await server.prompt("hello")
        finally:
            await server.stop()

    response = run(scenario())
    assert response["success"] is False
    assert response["error"] == "Timeout after 120s"
    assert response["text"] is None
    assert response["images"] == []


def test_ws_front_reports_router_exceptions_instead_of_hanging():
    """A crash in the core must still produce a `response`, or the CLI waits forever."""
    router = StubRouter(raises=RuntimeError("upstream exploded"))

    async def scenario():
        server = FakeCliServer(free_port())
        await server.start()
        try:
            async with ws_front(server.port, router):
                await server.wait_connected()
                return await server.prompt("hello")
        finally:
            await server.stop()

    response = run(scenario())
    assert response["success"] is False
    assert "upstream exploded" in response["error"]


def test_ws_front_handles_prompts_concurrently():
    """The read loop must not serialise prompts (bridges accept concurrent ones)."""
    gate = asyncio.Event()
    router = StubRouter(gate=gate)

    async def scenario():
        router.gate = asyncio.Event()  # bound to this test's loop
        server = FakeCliServer(free_port())
        await server.start()
        try:
            async with ws_front(server.port, router):
                await server.wait_connected()
                _, first = await server.send_prompt("one")
                _, second = await server.send_prompt("two")
                # Both must reach the router while neither can finish.
                assert await wait_for(lambda: router.started == 2, timeout=10)
                router.gate.set()
                return await asyncio.gather(
                    asyncio.wait_for(first, 15), asyncio.wait_for(second, 15))
        finally:
            await server.stop()

    first, second = run(scenario())
    assert first["success"] and second["success"]
    assert router.max_in_flight >= 2


def test_ws_front_reconnects_after_the_server_restarts():
    """The CLI is short-lived: its server disappearing is routine, not fatal."""
    router = StubRouter()

    async def scenario():
        port = free_port()
        server = FakeCliServer(port)
        await server.start()
        front = WebSocketFront([f"ws://127.0.0.1:{port}"], router)
        await front.start()
        try:
            # The server sees the handshake a scheduling turn before the front's
            # own task resumes, so poll rather than assert immediately.
            assert await wait_for(lambda: front.snapshot()[0]["connected"] is True,
                                  timeout=10)

            await server.stop()
            assert await wait_for(lambda: front.snapshot()[0]["connected"] is False,
                                  timeout=10)

            # Same port, brand-new server: the front should dial back in on its own.
            server = FakeCliServer(port)
            await server.start()
            await server.wait_connected()
            assert await wait_for(lambda: front.snapshot()[0]["connected"] is True,
                                  timeout=20)
            return await server.prompt("after restart")
        finally:
            await front.stop()
            await server.stop()

    response = run(scenario())
    assert response["success"] is True
    assert router.requests[-1].text == "after restart"


def test_ws_front_snapshot_reports_unreachable_targets():
    """`GET /upstreams` shows why a target is down without needing the logs."""
    router = StubRouter()

    async def scenario():
        # Nothing is listening on this port; the front must keep retrying quietly.
        front = WebSocketFront([f"ws://127.0.0.1:{free_port()}"], router,
                               fast_reconnect=True)
        await front.start()
        try:
            await wait_for(lambda: front.snapshot()[0]["attempts"] >= 1, timeout=10)
            return front.snapshot()
        finally:
            await front.stop()

    snapshot = run(scenario())
    assert len(snapshot) == 1
    assert set(snapshot[0]) == {"url", "connected", "last_error", "attempts"}
    assert snapshot[0]["connected"] is False
    assert snapshot[0]["attempts"] >= 1
    assert snapshot[0]["last_error"]


def test_ws_front_reconnect_cadence_matches_the_extension():
    """1 s, 2 s, 4 s … capped at 30 s; a flat 500 ms in fast mode.

    Mirrors background.js:scheduleReconnect(); asserted directly so the integration
    test above does not have to sit through a real backoff to prove it.
    """
    target = ws_client._WsTarget("ws://127.0.0.1:1", StubRouter(), 120.0,
                                 fast_reconnect=False)
    delays = []
    for _ in range(8):
        target.attempts += 1          # what a failed dial does
        delays.append(target._next_delay())
    assert delays == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0]

    # Fast mode is the default here exactly as `fastReconnect = true` is in
    # background.js, so an unconfigured front behaves like an unconfigured browser.
    fast = ws_client._WsTarget("ws://127.0.0.1:1", StubRouter(), 120.0)
    fast.attempts += 5
    assert fast._next_delay() == 0.5


def test_ws_front_accepts_config_style_targets():
    """`router.json` stores ws_targets as objects; plain strings also work."""
    front = WebSocketFront([{"url": "ws://127.0.0.1:8760"}, "ws://127.0.0.1:8761"],
                           StubRouter())
    assert [entry["url"] for entry in front.snapshot()] == [
        "ws://127.0.0.1:8760", "ws://127.0.0.1:8761"]


def test_ws_front_with_no_targets_is_a_no_op():
    async def scenario():
        front = WebSocketFront([], StubRouter())
        await front.start()
        await front.stop()
        return front.snapshot()

    assert run(scenario()) == []


# ============ Unix-socket front ===============================================

def test_unix_front_prompt_round_trip_through_the_real_cli(sock_path):
    """Driven by ai-prompt-native.py itself, so the framing under test is the real one."""
    router = StubRouter(PromptResponse(
        success=True, text="pong", session_id="sess_9", provider="chatgpt"))
    path = sock_path

    async def scenario():
        async with unix_front(path, router):
            return await native_cli.send_and_receive(
                {"type": "prompt", "request_id": "req-1", "text": "ping",
                 "timeout": 42, "ephemeral": True},
                timeout=15, socket_path=str(path))

    response = run(scenario())
    assert response["type"] == "response"
    assert response["request_id"] == "req-1"
    assert response["success"] is True
    assert response["text"] == "pong"
    assert response["session_id"] == "sess_9"
    assert "error" not in response

    request = router.requests[-1]
    assert request.text == "ping"
    assert request.ephemeral is True
    # Unlike the WebSocket front, this one honours the message's timeout (--timeout).
    assert request.timeout == 42.0
    assert router.calls[-1] == {"hops": 0, "via": None}


@pytest.mark.parametrize("healthy", [True, False])
def test_unix_front_status_reports_any_healthy(sock_path, healthy):
    """`ai-prompt-native.py --status` asks this question; §5.5 defines the answer."""
    router = StubRouter(healthy=healthy)
    path = sock_path

    async def scenario():
        async with unix_front(path, router):
            return await native_cli.send_and_receive(
                {"type": "status", "request_id": "status"},
                timeout=15, socket_path=str(path))

    response = run(scenario())
    assert response == {"type": "status", "request_id": "status", "connected": healthy}
    assert router.requests == []  # a status must never reach forward_prompt


def test_unix_front_defaults_the_timeout_when_the_client_omits_it(sock_path):
    router = StubRouter()
    path = sock_path

    async def scenario():
        async with unix_front(path, router):
            return await raw_unix_request(
                path, {"type": "prompt", "request_id": "r", "text": "hi"})

    assert run(scenario())["success"] is True
    assert router.requests[-1].timeout == 120.0


def test_unix_front_socket_is_0600_and_cleaned_up(sock_path):
    """The file mode *is* the auth on this front (plan §6)."""
    path = sock_path
    path.write_text("stale socket file left by a crashed router")
    router = StubRouter()

    async def scenario():
        front = UnixSocketFront(str(path), router)
        await front.start()
        mode = os.stat(str(path)).st_mode
        listening = front.snapshot()
        await front.stop()
        return mode, listening, front.snapshot()

    mode, listening, stopped = run(scenario())
    assert stat.S_ISSOCK(mode)                       # the stale plain file was replaced
    assert stat.S_IMODE(mode) == 0o600
    assert listening == {"path": str(path), "listening": True, "connections": 0}
    assert stopped["listening"] is False
    assert not path.exists()                         # unlinked on stop


def test_unix_front_handles_concurrent_clients(sock_path):
    router = StubRouter()
    path = sock_path

    async def scenario():
        router.gate = asyncio.Event()
        async with unix_front(path, router):
            calls = [raw_unix_request(path,
                                      {"type": "prompt", "request_id": f"r{i}",
                                       "text": f"prompt {i}", "timeout": 30})
                     for i in range(4)]
            task = asyncio.gather(*calls)
            assert await wait_for(lambda: router.started == 4, timeout=10)
            router.gate.set()
            return await asyncio.wait_for(task, 15)

    responses = run(scenario())
    assert [r["request_id"] for r in responses] == ["r0", "r1", "r2", "r3"]
    assert all(r["success"] for r in responses)
    assert router.max_in_flight >= 2


def test_unix_front_survives_a_bad_client(sock_path):
    """A truncated frame, a bogus length and junk JSON must not take the server down."""
    router = StubRouter()
    path = sock_path

    async def scenario():
        async with unix_front(path, router) as front:
            # 1. Half a length prefix, then hang up.
            reader, writer = await asyncio.open_unix_connection(str(path))
            writer.write(b"\x02\x00")
            await writer.drain()
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

            # 2. A length prefix promising far more than we will ever accept.
            reader, writer = await asyncio.open_unix_connection(str(path))
            writer.write(struct.pack("<I", 4_000_000_000))
            await writer.drain()
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

            # 3. Correct framing, garbage payload: answered, connection kept open.
            reader, writer = await asyncio.open_unix_connection(str(path))
            try:
                junk = b"not json at all"
                writer.write(struct.pack("<I", len(junk)) + junk)
                await writer.drain()
                header = await asyncio.wait_for(reader.readexactly(4), 15)
                (length,) = struct.unpack("<I", header)
                bad = json.loads((await reader.readexactly(length)).decode("utf-8"))
            finally:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()

            # 4. The server is still healthy for a well-behaved client.
            good = await native_cli.send_and_receive(
                {"type": "prompt", "request_id": "ok", "text": "still there?"},
                timeout=15, socket_path=str(path))
            # Handlers notice a hang-up on their next read, so the count settles a
            # tick after the client is gone -- poll instead of sampling once.
            released = await wait_for(
                lambda: front.snapshot()["connections"] == 0, timeout=10)
            return bad, good, front.snapshot(), released

    bad, good, snapshot, released = run(scenario())
    assert bad["success"] is False and "Invalid JSON" in bad["error"]
    assert good["success"] is True
    assert snapshot["listening"] is True
    assert released  # every client was released


def test_unix_front_rejects_an_unusable_prompt_without_dying(sock_path):
    """Empty text fails PromptRequest validation; the client still gets an answer."""
    router = StubRouter()
    path = sock_path

    async def scenario():
        async with unix_front(path, router):
            return await raw_unix_request(
                path, {"type": "prompt", "request_id": "empty", "text": ""})

    response = run(scenario())
    assert response["success"] is False
    assert "Invalid prompt message" in response["error"]
    assert router.requests == []


def test_unix_front_restart_reclaims_its_own_socket(sock_path):
    """A router restart must not be blocked by the socket file it left behind.

    A hard kill leaves the AF_UNIX file on disk with nothing accepting on it, and a
    bind() onto it fails with EADDRINUSE. Reproduce that state exactly: bind a
    socket, drop it without unlinking, then start the front on the same path.
    """
    router = StubRouter()
    path = sock_path

    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(path))
    stale.close()
    assert path.exists()

    async def scenario():
        async with unix_front(path, router):
            return await raw_unix_request(
                path, {"type": "prompt", "request_id": "again", "text": "hello"})

    assert run(scenario())["success"] is True


# ============ ai-prompt-native.py --socket ====================================

def test_native_cli_socket_precedence(monkeypatch):
    """--socket > $AI_PROMPT_NATIVE_SOCKET > the native host's default."""
    monkeypatch.delenv("AI_PROMPT_NATIVE_SOCKET", raising=False)
    assert native_cli.resolve_socket_path() == native_cli.DEFAULT_SOCKET_PATH
    assert native_cli.resolve_socket_path("/tmp/explicit.sock") == "/tmp/explicit.sock"

    monkeypatch.setenv("AI_PROMPT_NATIVE_SOCKET", "/tmp/from-env.sock")
    assert native_cli.resolve_socket_path() == "/tmp/from-env.sock"
    assert native_cli.resolve_socket_path("/tmp/explicit.sock") == "/tmp/explicit.sock"


def test_native_cli_keeps_its_flags_and_default_error(sock_path, monkeypatch):
    """Existing flags/output must not shift; --socket is additive."""
    monkeypatch.delenv("AI_PROMPT_NATIVE_SOCKET", raising=False)
    parser_flags = _native_cli_option_strings()
    for flag in ["--timeout", "--session", "-s", "--provider", "-p", "--json", "-j",
                 "--status", "--socket"]:
        assert flag in parser_flags

    # The wording for the default socket is unchanged...
    missing_default = run(native_cli.send_and_receive(
        {"type": "status", "request_id": "s"}, timeout=1,
        socket_path=native_cli.DEFAULT_SOCKET_PATH + ".nope"))
    assert missing_default["success"] is False

    absent = sock_path.parent / "absent.sock"
    named = run(native_cli.send_and_receive({"type": "status", "request_id": "s"},
                                            timeout=1, socket_path=str(absent)))
    assert absent.name in named["error"]


def _native_cli_option_strings():
    """Every flag ai-prompt-native.py's parser accepts, via a dry --help build."""
    import argparse

    captured = {}
    real_parse = argparse.ArgumentParser.parse_args

    def fake_parse(self, *args, **kwargs):
        captured["flags"] = [opt for action in self._actions
                             for opt in action.option_strings]
        raise SystemExit(0)

    argparse.ArgumentParser.parse_args = fake_parse
    try:
        with contextlib.suppress(SystemExit):
            native_cli.main()
    finally:
        argparse.ArgumentParser.parse_args = real_parse
    return captured.get("flags", [])


# ============ contract check against the real core ============================

def test_real_router_satisfies_the_contract_the_fronts_call():
    """Skips while the core is still being written; fails loudly if it drifts."""
    router_module = pytest.importorskip("ai_prompt_router.router")
    Router = getattr(router_module, "Router", None)
    if Router is None:
        pytest.skip("ai_prompt_router.router.Router not implemented yet")

    assert callable(getattr(Router, "any_healthy", None))
    signature = inspect.signature(Router.forward_prompt)
    assert "hops" in signature.parameters
    assert "via" in signature.parameters
