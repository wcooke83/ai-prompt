"""FastAPI app exposing the bridge's contract, backed by the router instead of a browser.

The four routes an app already talks to (/prompt, /sessions, /status, /health) keep
their exact shapes -- models are imported from ai_prompt_api.models -- so pointing a
caller at the router is a host/port/key change and nothing else. Two additions are
response headers only, invisible to a JSON-parsing client:

    X-AI-Prompt-Upstream  which bridge served the request
    X-AI-Prompt-Client    which configured api_key name called us
    X-AI-Prompt-Attempts  the upstreams tried, in order (diagnostics for failover)

/upstreams is the one route the bridge does not have: the operator's dashboard.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response

from ai_prompt_api.models import (
    DeleteResponse,
    HealthResponse,
    PromptRequest,
    PromptResponse,
    SessionInfo,
    SessionListResponse,
    StatusResponse,
)

from . import state
from .auth import require_api_key
from .router import LoopDetected, NoUpstreamAvailable, Router, parse_hops, parse_via

logger = logging.getLogger(__name__)

# The bridge's wording, kept verbatim: callers already branch on this string when a
# browser is missing, and the router must not invent a new one.
NO_UPSTREAM_DETAIL = "Browser extension not connected"


def _router(request: Request) -> Router:
    router = state.router_for(request)
    if router is None:
        raise HTTPException(status_code=503, detail=NO_UPSTREAM_DETAIL)
    return router


def _fronts_snapshot() -> dict[str, Any]:
    """Built defensively: the fronts are optional and may not be running at all."""
    fronts: dict[str, Any] = {"websocket": [], "unix_socket": None}

    ws_front = getattr(state, "ws_front", None)
    if ws_front is not None:
        try:
            fronts["websocket"] = ws_front.snapshot()
        except Exception as exc:  # noqa: BLE001 - a broken front must not break /upstreams
            logger.warning("ws front snapshot failed: %s", exc)
            fronts["websocket"] = {"error": str(exc)}

    unix_front = getattr(state, "unix_front", None)
    if unix_front is not None:
        try:
            fronts["unix_socket"] = unix_front.snapshot()
        except Exception as exc:  # noqa: BLE001
            logger.warning("unix front snapshot failed: %s", exc)
            fronts["unix_socket"] = {"error": str(exc)}

    return fronts


def create_app(router: Optional[Router] = None, *, lifespan=None) -> FastAPI:
    app = FastAPI(
        title="AI Prompt Router",
        description="Round-robin front for several ai-prompt bridges",
        version="1.0.0",
        lifespan=lifespan,
    )
    if router is not None:
        app.state.router = router

    @app.middleware("http")
    async def echo_client_name(request: Request, call_next):
        """Echo the authenticated app name on every response, errors included.

        The dependency stores it on request.state, which is backed by the ASGI scope,
        so it is the same object the endpoint wrote to.
        """
        response = await call_next(request)
        client_name = getattr(request.state, "client_name", None)
        if client_name:
            response.headers["X-AI-Prompt-Client"] = client_name
        return response

    @app.post("/prompt", response_model=PromptResponse, tags=["prompts"])
    async def send_prompt(
        payload: PromptRequest,
        request: Request,
        response: Response,
        _client: Optional[str] = Depends(require_api_key),
    ):
        router = _router(request)
        try:
            result = await router.forward_prompt(
                payload,
                hops=parse_hops(request.headers.get("X-AI-Prompt-Hops")),
                via=parse_via(request.headers.get("X-AI-Prompt-Via")),
            )
        except LoopDetected as exc:
            raise HTTPException(status_code=508, detail=str(exc)) from exc
        except NoUpstreamAvailable as exc:
            logger.warning("no upstream available: %s", exc)
            raise HTTPException(status_code=503, detail=NO_UPSTREAM_DETAIL) from exc

        if result.upstream:
            response.headers["X-AI-Prompt-Upstream"] = result.upstream
        if result.attempts:
            response.headers["X-AI-Prompt-Attempts"] = ",".join(result.attempts)
        return result.response

    @app.get("/sessions", response_model=SessionListResponse, tags=["sessions"])
    async def list_sessions(
        request: Request,
        _client: Optional[str] = Depends(require_api_key),
    ):
        router = _router(request)
        return SessionListResponse(sessions=await router.list_sessions())

    @app.get("/sessions/{session_id}", response_model=SessionInfo, tags=["sessions"])
    async def get_session(
        session_id: str,
        request: Request,
        _client: Optional[str] = Depends(require_api_key),
    ):
        router = _router(request)
        info = await router.get_session(session_id)
        if info is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return info

    @app.delete("/sessions/{session_id}", response_model=DeleteResponse, tags=["sessions"])
    async def delete_session(
        session_id: str,
        request: Request,
        _client: Optional[str] = Depends(require_api_key),
    ):
        router = _router(request)
        if not await router.delete_session(session_id):
            raise HTTPException(status_code=404, detail="Session not found")
        return DeleteResponse(success=True)

    @app.get("/health", response_model=HealthResponse, tags=["health"])
    async def health(request: Request):
        """Unauthenticated, like the bridge's -- and the probe a parent router polls.

        extension_connected = "any upstream healthy", which is what makes a chain of
        routers compose: a parent treats us exactly as it treats a bridge.
        """
        router = state.router_for(request)
        return HealthResponse(
            status="ok",
            extension_connected=bool(router and router.any_healthy()),
        )

    @app.get("/status", response_model=StatusResponse, tags=["health"])
    async def status(request: Request):
        router = state.router_for(request)
        if router is None:
            return StatusResponse(
                extension_connected=False,
                active_provider=None,
                available_providers=[],
                pending_requests=0,
                active_sessions=0,
            )
        return router.status_snapshot()

    @app.get("/upstreams", tags=["router"])
    async def upstreams(
        request: Request,
        _client: Optional[str] = Depends(require_api_key),
    ):
        router = _router(request)
        return {
            "router": {
                "instance_id": router.instance_id,
                "strategy": router.config.strategy,
                "failover": router.config.failover,
                "max_attempts": router.config.max_attempts,
                "max_hops": router.config.max_hops,
            },
            "upstreams": router.upstreams_snapshot(),
            "fronts": _fronts_snapshot(),
        }

    return app


# Module-level app for `uvicorn ai_prompt_router.app:app`; it reads state.router, which
# __main__ populates. Building it here is free (no config is read at import time).
app = create_app()
