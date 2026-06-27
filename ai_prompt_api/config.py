"""API configuration settings."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    ws_port: int = 8760
    default_timeout: float = 120.0
    host: str = "0.0.0.0"
    port: int = 8000
    api_key: str | None = None  # Set AI_PROMPT_API_KEY to enable auth

    class Config:
        env_prefix = "AI_PROMPT_"
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
