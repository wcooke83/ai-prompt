"""Session management endpoints."""

from fastapi import APIRouter, Depends, HTTPException

from ..auth import require_api_key
from ..models import DeleteResponse, SessionInfo, SessionListResponse

router = APIRouter(prefix="/sessions", tags=["sessions"], dependencies=[Depends(require_api_key)])


@router.get("", response_model=SessionListResponse)
async def list_sessions():
    """List all active sessions."""
    from ..main import extension_manager

    if not extension_manager:
        return SessionListResponse(sessions=[])

    sessions = [
        SessionInfo(
            session_id=sid,
            provider=info.get("provider"),
            created_at=info.get("created_at"),
            active=True,
        )
        for sid, info in extension_manager.list_sessions().items()
    ]
    return SessionListResponse(sessions=sessions)


@router.get("/{session_id}", response_model=SessionInfo)
async def get_session(session_id: str):
    """Get session info by ID."""
    from ..main import extension_manager

    if not extension_manager:
        raise HTTPException(status_code=404, detail="Session not found")

    info = extension_manager.get_session(session_id)
    if not info:
        raise HTTPException(status_code=404, detail="Session not found")

    return SessionInfo(
        session_id=session_id,
        provider=info.get("provider"),
        created_at=info.get("created_at"),
        active=True,
    )


@router.delete("/{session_id}", response_model=DeleteResponse)
async def delete_session(session_id: str):
    """Delete a session."""
    from ..main import extension_manager

    if not extension_manager:
        raise HTTPException(status_code=404, detail="Session not found")

    success = extension_manager.delete_session(session_id)
    if not success:
        raise HTTPException(status_code=404, detail="Session not found")

    return DeleteResponse(success=True)
