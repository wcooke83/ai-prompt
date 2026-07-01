"""API configuration settings."""

from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    ws_port: int = 8760
    default_timeout: float = 120.0
    host: str = "0.0.0.0"
    port: int = 8000
    api_key: Optional[str] = None  # Set AI_PROMPT_API_KEY to enable auth

    class Config:
        env_prefix = "AI_PROMPT_"
        # Anchored to this file's own directory rather than a bare relative path — a bare
        # ".env" resolves against the launching process's cwd, which for this bridge is
        # whatever directory started uvicorn (e.g. another repo's start script), not
        # necessarily this one. That previously caused an unrelated project's .env to be
        # loaded here instead, crashing startup on its extra keys.
        env_file = str(Path(__file__).resolve().parent / ".env")
        env_file_encoding = "utf-8"


settings = Settings()
