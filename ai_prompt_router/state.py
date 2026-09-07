"""Module-level handles to the live router and its fronts.

Mirrors ai_prompt_api/state.py: the HTTP routes, the WebSocket front and the
unix-socket front all reach the one running Router through this module instead of
being wired to each other.

    router      -- Router | None
    ws_front    -- fronts.ws_client.WebSocketFront | None
    unix_front  -- fronts.unix_socket.UnixSocketFront | None
"""

from __future__ import annotations

from typing import Any, Optional

router: Optional[Any] = None
ws_front: Optional[Any] = None
unix_front: Optional[Any] = None


def router_for(request: Any) -> Optional[Any]:
    """Resolve the Router serving one HTTP request.

    Prefers `request.app.state.router` and falls back to the module global. The
    per-app handle exists so more than one router can live in a single process --
    which the router-to-router chain tests do, and which a future in-process
    embedding would need. Normal deployments only ever use the global.
    """
    app = getattr(request, "app", None)
    app_state = getattr(app, "state", None)
    scoped = getattr(app_state, "router", None) if app_state is not None else None
    return scoped if scoped is not None else router
