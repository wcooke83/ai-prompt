"""Health and status endpoints."""

from fastapi import APIRouter

from ..models import HealthResponse, Provider, StatusResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health():
    """Check API health and extension connection status."""
    from ..main import extension_manager

    return HealthResponse(
        status="ok",
        extension_connected=extension_manager.is_connected if extension_manager else False,
    )


@router.get("/status", response_model=StatusResponse)
async def status():
    """Get detailed API status."""
    from ..main import extension_manager

    return StatusResponse(
        extension_connected=extension_manager.is_connected if extension_manager else False,
        active_provider=None,
        available_providers=[p.value for p in Provider],
        pending_requests=len(extension_manager.pending_requests) if extension_manager else 0,
        active_sessions=len(extension_manager.sessions) if extension_manager else 0,
    )
