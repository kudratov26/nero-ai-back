"""FastAPI application factory."""

from __future__ import annotations

from fastapi import FastAPI

from app.api.health import router as health_router
from app.api.problems import register_exception_handlers
from app.api.request_context import RequestContextMiddleware
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI app. Takes explicit settings in tests."""
    resolved = settings if settings is not None else get_settings()
    configure_logging(resolved)

    app = FastAPI(
        title="Nero AI API",
        version="1.0.0",
        description="Accounts, Credits, Chats, Generations, and provider access.",
        docs_url=None if resolved.is_production else "/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
    )
    app.state.settings = resolved
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)
    app.include_router(health_router)
    return app


app = create_app()
