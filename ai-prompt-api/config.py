"""API configuration settings."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    ws_port: int = 8760
    default_timeout: float = 120.0
    host: str = "0.0.0.0"
    port: int = 8000

    class Config:
        env_prefix = "AI_PROMPT_"


settings = Settings()
