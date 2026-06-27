"""Prompt submission endpoint."""

from fastapi import APIRouter, Depends, HTTPException

from ..auth import require_api_key
from ..config import settings
from ..models import ImageData, PromptRequest, PromptResponse

router = APIRouter(tags=["prompts"], dependencies=[Depends(require_api_key)])


@router.post("/prompt", response_model=PromptResponse)
async def send_prompt(request: PromptRequest):
    """Send a prompt to the AI provider via browser extension."""
    from ..main import extension_manager

    if not extension_manager or not extension_manager.is_connected:
        raise HTTPException(status_code=503, detail="Browser extension not connected")

    timeout = request.timeout or settings.default_timeout

    # Convert attachments to dict format
    attachments = None
    if request.attachments:
        attachments = [att.model_dump() for att in request.attachments]

    result = await extension_manager.send_prompt(
        text=request.text,
        session_id=request.session_id,
        provider=request.provider.value if request.provider else None,
        ephemeral=request.ephemeral or False,
        timeout=timeout,
        attachments=attachments,
    )

    images = None
    if result.get("images"):
        images = [
            ImageData(
                src=img.get("src", img) if isinstance(img, dict) else img,
                alt=img.get("alt") if isinstance(img, dict) else None,
            )
            for img in result["images"]
        ]

    return PromptResponse(
        success=result.get("success", False),
        text=result.get("text"),
        session_id=result.get("session_id"),
        provider=result.get("provider"),
        model=result.get("model"),
        input_tokens=result.get("input_tokens"),
        output_tokens=result.get("output_tokens"),
        images=images,
        error=result.get("error"),
    )
