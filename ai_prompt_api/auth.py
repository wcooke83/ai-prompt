"""API key authentication dependency."""

from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import settings

security = HTTPBearer(auto_error=False)


async def require_api_key(
    credentials: HTTPAuthorizationCredentials | None = Security(security),
) -> None:
    """Verify API key if one is configured. No-op when api_key is not set."""
    if not settings.api_key:
        return  # Auth disabled (local dev)

    if not credentials or credentials.credentials != settings.api_key:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
