"""FastAPI application entry point."""

from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI

from .config import settings
from .routers import health, prompts, sessions
from .services.extension import ExtensionManager

extension_manager: Optional[ExtensionManager] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage WebSocket server lifecycle."""
    global extension_manager
    extension_manager = ExtensionManager(port=settings.ws_port)
    await extension_manager.start()
    yield
    await extension_manager.stop()


app = FastAPI(
    title="AI Prompt API",
    description="REST API for sending prompts to AI providers via browser extension",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(health.router)
app.include_router(prompts.router)
app.include_router(sessions.router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "ai_prompt_api.main:app",
        host=settings.host,
        port=settings.port,
        reload=True,
    )
