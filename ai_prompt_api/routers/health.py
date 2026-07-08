"""Health and status endpoints."""

from fastapi import APIRouter

from .. import state
from ..models import HealthResponse, Provider, StatusResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health():
    """Check API health and extension connection status."""
    mgr = state.extension_manager

    return HealthResponse(
        status="ok",
        extension_connected=mgr.is_connected if mgr else False,
    )


@router.get("/status", response_model=StatusResponse)
async def status():
    """Get detailed API status."""
    mgr = state.extension_manager

    return StatusResponse(
        extension_connected=mgr.is_connected if mgr else False,
        active_provider=None,
        available_providers=[p.value for p in Provider],
        pending_requests=len(mgr.pending_requests) if mgr else 0,
        active_sessions=len(mgr.sessions) if mgr else 0,
    )
