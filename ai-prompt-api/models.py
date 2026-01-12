"""Pydantic request/response models."""

from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class Provider(str, Enum):
    chatgpt = "chatgpt"
    claude = "claude"
    grok = "grok"


class Attachment(BaseModel):
    name: str = Field(..., description="Filename")
    type: str = Field(..., description="MIME type")
    data: str = Field(..., description="Base64-encoded file content")


class PromptRequest(BaseModel):
    text: str = Field(..., min_length=1)
    provider: Optional[Provider] = None
    session_id: Optional[str] = None
    timeout: Optional[float] = Field(None, gt=0, le=600)
    ephemeral: Optional[bool] = False
    attachments: Optional[List[Attachment]] = Field(None, description="File attachments (currently supported: Grok)")


class ImageData(BaseModel):
    src: str
    alt: Optional[str] = None


class PromptResponse(BaseModel):
    success: bool
    text: Optional[str] = None
    session_id: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    images: Optional[List[ImageData]] = None
    error: Optional[str] = None


class HealthResponse(BaseModel):
    status: str
    extension_connected: bool
    active_provider: Optional[str] = None


class StatusResponse(BaseModel):
    extension_connected: bool
    active_provider: Optional[str] = None
    available_providers: List[str]
    pending_requests: int
    active_sessions: int


class SessionInfo(BaseModel):
    session_id: str
    provider: Optional[str] = None
    created_at: datetime
    active: bool = True


class SessionListResponse(BaseModel):
    sessions: List[SessionInfo]


class DeleteResponse(BaseModel):
    success: bool
