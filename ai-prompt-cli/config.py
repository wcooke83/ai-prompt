"""Configuration management for ai-prompt CLI."""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from the cli directory
CLI_DIR = Path(__file__).parent
load_dotenv(CLI_DIR / ".env")


def get_config():
    """Get configuration from environment variables with defaults."""
    log_dir = os.getenv("AI_PROMPT_LOG_DIR", str(CLI_DIR / "logs"))
    # Resolve relative paths relative to CLI directory
    log_dir_path = Path(log_dir)
    if not log_dir_path.is_absolute():
        log_dir = str(CLI_DIR / log_dir_path)

    # Queue directory
    queue_dir = os.getenv("AI_PROMPT_QUEUE_DIR", str(CLI_DIR / "queue"))
    queue_dir_path = Path(queue_dir)
    if not queue_dir_path.is_absolute():
        queue_dir = str(CLI_DIR / queue_dir_path)

    return {
        "port": int(os.getenv("AI_PROMPT_PORT", "8760")),
        "timeout": float(os.getenv("AI_PROMPT_TIMEOUT", "120")),
        "connection_timeout": float(os.getenv("AI_PROMPT_CONNECTION_TIMEOUT", "30")),
        "response_timeout": float(os.getenv("AI_PROMPT_RESPONSE_TIMEOUT", "120")),
        "log_enabled": os.getenv("AI_PROMPT_LOG_ENABLED", "false").lower() == "true",
        "log_dir": log_dir,
        "default_provider": os.getenv("AI_PROMPT_DEFAULT_PROVIDER", ""),
        "queue_dir": queue_dir,
        "max_queue_size": int(os.getenv("AI_PROMPT_MAX_QUEUE_SIZE", "10")),
    }