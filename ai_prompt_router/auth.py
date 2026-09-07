"""Bearer auth for the router's HTTP front, against the per-app keys in router.json.

Same rule as the bridge: no keys configured means no auth (local dev). Unlike the
bridge there are several valid keys, each with a name, so a request can be attributed
to an app -- the name is echoed as X-AI-Prompt-Client and logged, which is what makes
`/upstreams` and the logs readable per caller. Revoking an app is deleting its entry
and restarting.
"""

from __future__ import annotations

import secrets
from typing import Optional

from fastapi import HTTPException, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import state

security = HTTPBearer(auto_error=False)


async def require_api_key(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Security(security),
) -> Optional[str]:
    """Return the calling app's name, or None when auth is disabled."""
    router = state.router_for(request)
    # No router wired yet (startup/shutdown window): the endpoint itself answers 503,
    # and there is nothing to protect because nothing can be forwarded.
    api_keys = router.config.api_keys if router is not None else []
    if not api_keys:
        return None

    provided = credentials.credentials if credentials else None
    if provided:
        for entry in api_keys:
            # compare_digest, not ==, so the comparison time does not leak how much of
            # a guessed key was right.
            if secrets.compare_digest(provided, entry.key):
                request.state.client_name = entry.name
                return entry.name

    raise HTTPException(status_code=401, detail="Invalid or missing API key")
