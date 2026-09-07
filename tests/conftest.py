"""Shared fixtures for the router tests.

Two kinds of fake upstream live here:

  ScriptedUpstream  -- an httpx.MockTransport that speaks the bridge's four routes and
                       can be told to misbehave in exactly one way (a status code, a
                       `success:false` body, a raised transport error). Used for the
                       unit-level rules: rotation, pins, the failover table.

  make_bridge_app   -- a *real* ai_prompt_api app (its actual routers) wired to a stub
                       extension manager. Used for the integration tests, so the
                       contract being asserted is the bridge's real one and not a
                       hand-written imitation that could drift from it.

Nothing here opens a socket: every upstream is reached through an httpx transport, so
the whole suite runs in one process with no ports and no browser.
"""

from __future__ import annotations

import collections
import contextlib
import json
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
import pytest

# The tests import both packages by their repo-root names; pytest only puts tests/ on
# sys.path, so the root has to be added explicitly.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ai_prompt_router.config import RouterConfig, UpstreamConfig  # noqa: E402
from ai_prompt_router.router import Router  # noqa: E402


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_response(status: int, payload: Any) -> httpx.Response:
    return httpx.Response(status_code=status, json=payload)


# --------------------------------------------------------------------------- #
# Scripted upstreams (httpx.MockTransport)
# --------------------------------------------------------------------------- #


class ScriptedUpstream:
    """A fake bridge whose prompt behaviour a test can replace outright.

    `connected` drives /health only, so a test can hand the router an upstream it
    believes is healthy and have the prompt itself fail -- which is the situation the
    whole failover table is about.
    """

    def __init__(
        self,
        name: str,
        *,
        connected: bool = True,
        prompt: Optional[Callable[["ScriptedUpstream", httpx.Request, dict], httpx.Response]] = None,
        providers: Optional[list[str]] = None,
        enabled: bool = True,
        api_key: Optional[str] = None,
        url: Optional[str] = None,
    ) -> None:
        self.name = name
        self.url = url or f"http://{name}.test"
        self.connected = connected
        self.prompt_handler = prompt
        self.providers = providers
        self.enabled = enabled
        self.api_key = api_key

        self.sessions: dict[str, dict] = {}
        self.prompt_bodies: list[dict] = []
        self.prompt_headers: list[httpx.Headers] = []
        self.calls: collections.Counter = collections.Counter()
        self._counter = 0

    # -- wiring ------------------------------------------------------------

    def config(self) -> UpstreamConfig:
        return UpstreamConfig(
            name=self.name,
            url=self.url,
            api_key=self.api_key,
            providers=self.providers,
            enabled=self.enabled,
        )

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def add_session(self, session_id: str, provider: str = "chatgpt") -> str:
        self.sessions[session_id] = {"provider": provider, "created_at": utcnow_iso()}
        return session_id

    # -- routes ------------------------------------------------------------

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method

        if path == "/health":
            self.calls["health"] += 1
            return json_response(200, {"status": "ok", "extension_connected": self.connected})

        if path == "/prompt" and method == "POST":
            self.calls["prompt"] += 1
            body = json.loads(request.content or b"{}")
            self.prompt_bodies.append(body)
            self.prompt_headers.append(request.headers)
            if self.prompt_handler is not None:
                return self.prompt_handler(self, request, body)
            return self.default_prompt(body)

        if path == "/sessions" and method == "GET":
            self.calls["list_sessions"] += 1
            return json_response(
                200,
                {
                    "sessions": [
                        {
                            "session_id": sid,
                            "provider": info["provider"],
                            "created_at": info["created_at"],
                            "active": True,
                        }
                        for sid, info in self.sessions.items()
                    ]
                },
            )

        if path.startswith("/sessions/"):
            session_id = path[len("/sessions/") :]
            if method == "GET":
                self.calls["get_session"] += 1
                info = self.sessions.get(session_id)
                if info is None:
                    return json_response(404, {"detail": "Session not found"})
                return json_response(
                    200,
                    {
                        "session_id": session_id,
                        "provider": info["provider"],
                        "created_at": info["created_at"],
                        "active": True,
                    },
                )
            if method == "DELETE":
                self.calls["delete_session"] += 1
                if self.sessions.pop(session_id, None) is None:
                    return json_response(404, {"detail": "Session not found"})
                return json_response(200, {"success": True})

        return json_response(404, {"detail": "not found"})

    def default_prompt(self, body: dict) -> httpx.Response:
        if not self.connected:
            return json_response(503, {"detail": "Browser extension not connected"})
        session_id = body.get("session_id")
        if not session_id:
            self._counter += 1
            session_id = f"{self.name}-s{self._counter}"
        provider = body.get("provider") or "chatgpt"
        self.sessions.setdefault(session_id, {"provider": provider, "created_at": utcnow_iso()})
        return json_response(
            200,
            {
                "success": True,
                "text": f"{self.name}: {body.get('text')}",
                "session_id": session_id,
                "provider": provider,
            },
        )


# -- prompt handler factories, one per row of the failover table ------------


def respond_status(status: int, payload: Optional[dict] = None):
    def handler(upstream, request, body):
        return json_response(status, payload if payload is not None else {"detail": f"HTTP {status}"})

    return handler


def respond_failure(error: str):
    """200 with `success:false` -- the bridge's normal way of reporting a failure."""

    def handler(upstream, request, body):
        return json_response(200, {"success": False, "error": error})

    return handler


def raise_transport(exc: Exception):
    def handler(upstream, request, body):
        raise exc

    return handler


# --------------------------------------------------------------------------- #
# Router assembly
# --------------------------------------------------------------------------- #


def make_config(upstreams, sessions_path: Path, **overrides) -> RouterConfig:
    return RouterConfig(
        upstreams=[u.config() if isinstance(u, ScriptedUpstream) else u for u in upstreams],
        sessions_path=sessions_path,
        **overrides,
    )


def build_router(stubs: list[ScriptedUpstream], sessions_path: Path, **overrides) -> Router:
    """A Router whose upstreams are the given stubs, keyed by url."""
    transports = {stub.url: stub.transport() for stub in stubs}
    config = make_config(stubs, sessions_path, **overrides)
    return Router(config, transport_factory=lambda upstream: transports.get(upstream.url))


@contextlib.asynccontextmanager
async def running(router: Router):
    await router.start()
    try:
        yield router
    finally:
        await router.stop()


@contextlib.asynccontextmanager
async def router_with(stubs: list[ScriptedUpstream], sessions_path: Path, **overrides):
    async with running(build_router(stubs, sessions_path, **overrides)) as router:
        yield router


@pytest.fixture
def sessions_path(tmp_path: Path) -> Path:
    return tmp_path / "router-sessions.json"


# --------------------------------------------------------------------------- #
# Real ai_prompt_api bridges, backed by a stub extension manager
# --------------------------------------------------------------------------- #

_current_stub: ContextVar[Optional["StubExtension"]] = ContextVar("current_stub", default=None)


class StubExtension:
    """Duck-types the interface ai_prompt_api/state.py documents."""

    def __init__(self, name: str, connected: bool = True) -> None:
        self.name = name
        self.is_connected = connected
        self.pending_requests: dict = {}
        self.sessions: dict = {}
        self.prompts: list[dict] = []
        self._counter = 0

    async def send_prompt(
        self,
        text: str,
        session_id: Optional[str] = None,
        provider: Optional[str] = None,
        ephemeral: bool = False,
        timeout: float = 120.0,
        attachments: Optional[list] = None,
    ) -> dict:
        self.prompts.append(
            {
                "text": text,
                "session_id": session_id,
                "provider": provider,
                "ephemeral": ephemeral,
                "timeout": timeout,
                "attachments": attachments,
            }
        )
        if not self.is_connected:
            return {"success": False, "error": "Extension not connected"}
        if not session_id:
            self._counter += 1
            session_id = f"{self.name}-s{self._counter}"
        resolved = provider or "chatgpt"
        self.sessions.setdefault(
            session_id, {"provider": resolved, "created_at": datetime.now(timezone.utc)}
        )
        return {
            "success": True,
            "text": f"{self.name}: {text}",
            "session_id": session_id,
            "provider": resolved,
        }

    def list_sessions(self) -> dict:
        return self.sessions

    def get_session(self, session_id: str) -> Optional[dict]:
        return self.sessions.get(session_id)

    def delete_session(self, session_id: str) -> bool:
        return self.sessions.pop(session_id, None) is not None


class _StubProxy:
    """Forwards every attribute to whichever stub owns the current request.

    ai_prompt_api's routers read one module-level `state.extension_manager`, so two
    bridge apps in one process would otherwise have to share a manager. A ContextVar
    set by the ASGI middleware below keeps them apart even when the router fans out to
    both concurrently, because each asyncio task gets its own copy of the context.
    """

    def __getattr__(self, item):
        stub = _current_stub.get()
        if stub is None:
            raise RuntimeError("no StubExtension bound to this request")
        return getattr(stub, item)


class _StubBinder:
    """Pure-ASGI middleware: sets the ContextVar in the same task that runs the route."""

    def __init__(self, app, stub: StubExtension) -> None:
        self.app = app
        self.stub = stub

    async def __call__(self, scope, receive, send):
        token = _current_stub.set(self.stub)
        try:
            await self.app(scope, receive, send)
        finally:
            _current_stub.reset(token)


def make_bridge_app(stub: StubExtension):
    """A FastAPI app built from the real ai_prompt_api routers."""
    from fastapi import FastAPI

    import ai_prompt_api.state as api_state
    from ai_prompt_api.routers import health, prompts, sessions

    if not isinstance(api_state.extension_manager, _StubProxy):
        api_state.extension_manager = _StubProxy()

    app = FastAPI()
    app.include_router(health.router)
    app.include_router(prompts.router)
    app.include_router(sessions.router)
    app.add_middleware(_StubBinder, stub=stub)
    return app


def asgi_transport(app) -> httpx.ASGITransport:
    return httpx.ASGITransport(app=app)


# --------------------------------------------------------------------------- #
# HTTP client against the router's own app
# --------------------------------------------------------------------------- #


def make_router_app(router: Router):
    from ai_prompt_router.app import create_app

    # The app is bound to this Router instance rather than the module global, so a
    # test can run two routers (a chain) in one process.
    return create_app(router=router)


@contextlib.asynccontextmanager
async def router_client(router: Router, **kwargs):
    app = make_router_app(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://router.test", **kwargs
    ) as client:
        yield client
