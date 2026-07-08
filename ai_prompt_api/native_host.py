#!/usr/bin/env python3
"""Browser-managed bridge: native-messaging host that also serves the REST API.

This is the recommended way to run the bridge. The browser launches this process
automatically (via the native-messaging manifest installed by
scripts/install-native-host.sh) whenever the extension connects, so there is no
separate start script to remember and no process to forget after a reboot.

One process, three surfaces — all sharing one request/response map:

  * native messaging (stdin/stdout) — the control channel to the extension. The
    extension performs the actual work (drives ChatGPT/Claude/Grok/DeepSeek tabs).
  * REST API (FastAPI/uvicorn) on an HTTP port the EXTENSION chooses and pushes
    down via a `configure` message — this is what apps (e.g. securities) POST to.
  * Unix socket — kept for the CLI's native mode (ai-prompt-cli/ai-prompt-native.py).

Because each browser profile spawns its own host, per-browser HTTP ports are set
once in each browser's extension popup ("HTTP API Port") with nothing to keep in
sync on the shell side.

CRITICAL: stdout is the native-messaging channel and must stay pristine — a stray
print()/log byte would corrupt the length-prefixed protocol. On startup we grab
the real stdout handle for framing and repoint sys.stdout at stderr so uvicorn's
logging (and any accidental print) can never poison the wire.
"""

import asyncio
import json
import os
import struct
import sys
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

# --- stdout hygiene (must happen before anything can write to stdout) ----------
# Real stdout: the native-messaging channel to the extension. Everything else
# (uvicorn logs, stray prints) is redirected to stderr so it can't corrupt framing.
_NM_OUT = sys.stdout.buffer
_NM_IN = sys.stdin.buffer
sys.stdout = sys.stderr

import uvicorn  # noqa: E402  (imported after stdout is made safe)
from fastapi import FastAPI  # noqa: E402

from . import state  # noqa: E402
from .config import settings  # noqa: E402
from .routers import health, prompts, sessions  # noqa: E402

SOCKET_PATH = os.path.join(tempfile.gettempdir(), "ai-prompt-native.sock")
DISCOVERY_DIR = Path.home() / ".ai-prompt" / "instances"


def log(*args):
    """Diagnostics go to stderr only (stdout is the native-messaging wire)."""
    print(f"[native-host {os.getpid()}]", *args, file=sys.stderr, flush=True)


# ============ Native messaging framing (4-byte little-endian length prefix) ====

def send_message_to_extension(message):
    """Write one framed JSON message to the extension over real stdout."""
    encoded = json.dumps(message).encode("utf-8")
    try:
        _NM_OUT.write(struct.pack("<I", len(encoded)))
        _NM_OUT.write(encoded)
        _NM_OUT.flush()
    except (BrokenPipeError, ValueError, OSError) as e:
        log("failed to write to extension:", e)


def read_message_from_extension():
    """Blocking read of one framed JSON message from the extension. None on EOF."""
    length_bytes = _NM_IN.read(4)
    if not length_bytes or len(length_bytes) < 4:
        return None
    length = struct.unpack("<I", length_bytes)[0]
    message_bytes = _NM_IN.read(length)
    if not message_bytes:
        return None
    return json.loads(message_bytes.decode("utf-8"))


# ============ Extension bridge (implements the state.extension_manager API) =====

class NativeExtensionBridge:
    """Talks to the extension over native messaging; same interface the routers
    expect from the WebSocket ExtensionManager."""

    def __init__(self, loop):
        self.loop = loop
        self.connected = False
        self.pending_requests = {}
        self.sessions = {}

    @property
    def is_connected(self) -> bool:
        return self.connected

    async def _request(self, message, timeout):
        """Send a prompt message to the extension and await its response."""
        if not self.connected:
            return {"success": False, "error": "Extension not connected"}

        request_id = message["request_id"]
        future = self.loop.create_future()
        self.pending_requests[request_id] = future
        try:
            send_message_to_extension(message)
            result = await asyncio.wait_for(future, timeout=timeout)
            if result.get("success") and result.get("session_id"):
                self.sessions[result["session_id"]] = {
                    "provider": result.get("provider"),
                    "created_at": datetime.now(),
                }
            return result
        except asyncio.TimeoutError:
            return {"success": False, "error": f"Timeout after {timeout}s"}
        finally:
            self.pending_requests.pop(request_id, None)

    async def send_prompt(self, text, session_id=None, provider=None,
                          ephemeral=False, timeout=120.0, attachments=None):
        message = {"type": "prompt", "request_id": str(uuid.uuid4()), "text": text}
        if session_id:
            message["session_id"] = session_id
        if provider:
            message["provider"] = provider
        if ephemeral:
            message["ephemeral"] = True
        if attachments:
            message["attachments"] = attachments
        return await self._request(message, timeout)

    def resolve(self, request_id, message):
        """Resolve a pending request with the extension's response."""
        future = self.pending_requests.get(request_id)
        if future and not future.done():
            future.set_result(message)

    # Session helpers (parity with ExtensionManager) --------------------------
    def get_session(self, session_id):
        return self.sessions.get(session_id)

    def list_sessions(self):
        return self.sessions

    def delete_session(self, session_id):
        if session_id in self.sessions:
            del self.sessions[session_id]
            return True
        return False


# ============ HTTP server (started on the port the extension configures) =======

def build_app() -> FastAPI:
    app = FastAPI(
        title="AI Prompt API (native host)",
        description="Browser-managed bridge to AI providers via the extension",
        version="1.0.0",
    )
    app.include_router(health.router)
    app.include_router(prompts.router)
    app.include_router(sessions.router)
    return app


async def _serve_guarded(server):
    """Run uvicorn's serve(), swallowing the SystemExit it raises on a bind failure.

    uvicorn calls sys.exit(1) when it can't bind. Inside an asyncio Task a SystemExit is re-raised
    into the event loop and would crash the whole host, so we turn it into a normal task completion;
    HttpServer.ensure() then detects the not-started task and reports a clean bind error.
    """
    try:
        await server.serve()
    except SystemExit:
        pass


class HttpServer:
    """A uvicorn server we can (re)bind to whatever port the extension asks for."""

    def __init__(self, app):
        self.app = app
        self.server = None
        self.task = None
        self.host = None
        self.port = None

    @property
    def running(self):
        return self.server is not None

    async def ensure(self, host, port):
        """Serve on host:port, restarting if the bind changed. No-op if unchanged."""
        if self.server is not None and self.host == host and self.port == port:
            return
        await self.stop()
        config = uvicorn.Config(
            self.app, host=host, port=port,
            log_level="warning", access_log=False,
        )
        server = uvicorn.Server(config)
        # Don't let uvicorn hijack process signals — the browser closing our stdin
        # (EOF) is our real shutdown trigger, handled in the read loop.
        server.install_signal_handlers = lambda: None
        self.server = server
        self.host, self.port = host, port
        self.task = asyncio.create_task(_serve_guarded(server))
        # serve() binds asynchronously, so a port conflict would otherwise leave a silently dead
        # HTTP server (and a discovery file that lies). Wait until it's really listening
        # (server.started) or the serve task ends without binding, and surface that as a real error.
        for _ in range(250):  # ~5s
            if server.started:
                break
            if self.task.done():
                self.server = self.task = self.host = self.port = None
                raise OSError(f"could not bind {host}:{port} (already in use?)")
            await asyncio.sleep(0.02)
        else:
            await self.stop()
            raise OSError(f"timed out binding {host}:{port}")
        log(f"HTTP API listening on {host}:{port}")

    async def stop(self):
        if self.server is not None:
            self.server.should_exit = True
        if self.task is not None:
            try:
                await asyncio.wait_for(self.task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        self.server = None
        self.task = None
        self.host = None
        self.port = None


# ============ Discovery file (lets tools enumerate live bridges) ===============

def write_discovery(bridge, http):
    """Best-effort record of this instance so other tools can find it."""
    try:
        DISCOVERY_DIR.mkdir(parents=True, exist_ok=True)
        path = DISCOVERY_DIR / f"{os.getpid()}.json"
        path.write_text(json.dumps({
            "pid": os.getpid(),
            "http_host": http.host,
            "http_port": http.port,
            "extension_connected": bridge.is_connected,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }))
    except OSError as e:
        log("discovery write failed:", e)


def remove_discovery():
    try:
        (DISCOVERY_DIR / f"{os.getpid()}.json").unlink()
    except OSError:
        pass


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def prune_stale_discovery():
    """Drop discovery files whose owning process is gone (host killed on reconnect / hard exit)."""
    try:
        for f in DISCOVERY_DIR.glob("*.json"):
            try:
                pid = int(f.stem)
            except ValueError:
                continue
            if pid != os.getpid() and not _pid_alive(pid):
                try:
                    f.unlink()
                except OSError:
                    pass
    except OSError:
        pass


# ============ Unix socket server (CLI native mode) =============================

async def handle_cli_client(reader, writer, bridge):
    try:
        while True:
            length_bytes = await reader.readexactly(4)
            length = struct.unpack("<I", length_bytes)[0]
            message = json.loads((await reader.readexactly(length)).decode("utf-8"))
            request_id = message.get("request_id")

            if message.get("type") == "status":
                response = {"type": "status", "request_id": request_id,
                            "connected": bridge.is_connected}
            else:
                response = await bridge._request(message, message.get("timeout", 120))

            payload = json.dumps(response).encode("utf-8")
            writer.write(struct.pack("<I", len(payload)))
            writer.write(payload)
            await writer.drain()
    except asyncio.IncompleteReadError:
        pass
    except Exception as e:  # noqa: BLE001 — keep the socket server alive
        log("CLI handler error:", e)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass


async def start_unix_server(bridge):
    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)
    server = await asyncio.start_unix_server(
        lambda r, w: handle_cli_client(r, w, bridge), SOCKET_PATH)
    os.chmod(SOCKET_PATH, 0o600)
    log(f"CLI unix socket at {SOCKET_PATH}")
    return server


# ============ Extension message handling ======================================

async def handle_extension_message(bridge, http, message):
    mtype = message.get("type")
    if mtype == "ready":
        bridge.connected = True
        log("extension ready")
        write_discovery(bridge, http)
    elif mtype == "configure":
        port = message.get("httpPort", message.get("http_port"))
        host = message.get("host", "0.0.0.0")
        if "apiKey" in message:
            settings.api_key = message["apiKey"] or None
        if port:
            try:
                await http.ensure(host, int(port))
                write_discovery(bridge, http)  # only claim the port once it's really bound
            except OSError as e:
                log(f"could not bind HTTP on {host}:{port} "
                    f"(is another browser/bridge already using it? give this browser its own port):", e)
        else:
            write_discovery(bridge, http)
    elif mtype == "response":
        bridge.resolve(message.get("request_id"), message)
    else:
        log("ignoring unknown message type:", mtype)


async def read_extension_loop(bridge, http):
    """Pump native-messaging input until the browser closes the pipe (EOF)."""
    loop = asyncio.get_running_loop()
    while True:
        message = await loop.run_in_executor(None, read_message_from_extension)
        if message is None:
            log("extension disconnected (stdin EOF)")
            bridge.connected = False
            return
        try:
            await handle_extension_message(bridge, http, message)
        except Exception as e:  # noqa: BLE001
            log("error handling extension message:", e)


async def amain():
    loop = asyncio.get_running_loop()
    bridge = NativeExtensionBridge(loop)
    state.extension_manager = bridge
    http = HttpServer(build_app())
    prune_stale_discovery()  # clear files left by hosts that died without cleaning up

    # Announce ourselves so the extension replies `ready` (and then `configure`).
    send_message_to_extension({"type": "host_ready"})

    unix_server = await start_unix_server(bridge)

    # Headless / testing escape hatch: bind HTTP immediately without waiting for the
    # extension's `configure`. NOT used in the browser-spawned path (which lets each
    # browser's popup dictate its own port, avoiding cross-profile port collisions).
    autostart_port = os.environ.get("AI_PROMPT_HOST_HTTP_PORT")
    if autostart_port:
        await http.ensure(os.environ.get("AI_PROMPT_HOST_HTTP_HOST", "0.0.0.0"),
                          int(autostart_port))

    try:
        await read_extension_loop(bridge, http)
    finally:
        await http.stop()
        unix_server.close()
        try:
            await unix_server.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)
        remove_discovery()


def main():
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
