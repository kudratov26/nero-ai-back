"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.auth import router as auth_router
from app.api.body_limit import RequestBodyLimitMiddleware
from app.api.catalog import router as catalog_router
from app.api.csrf import OriginCheckMiddleware
from app.api.health import router as health_router
from app.api.problems import register_exception_handlers
from app.api.request_context import RequestContextMiddleware
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.core.rate_limit import MemoryRateLimiter
from app.db.session import create_engine, create_session_factory


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI app. Takes explicit settings in tests."""
    resolved = settings if settings is not None else get_settings()
    configure_logging(resolved)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(resolved.database_url)
        app.state.engine = engine
        app.state.session_factory = create_session_factory(engine)
        try:
            yield
        finally:
            await engine.dispose()

    app = FastAPI(
        title="Nero AI API",
        version="1.0.0",
        description="Accounts, Credits, Chats, Generations, and provider access.",
        docs_url=None if resolved.is_production else "/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )
    app.state.settings = resolved
    app.state.rate_limiter = MemoryRateLimiter()
    # Added outermost first: the last `add_middleware` call runs first, so the
    # request id and access log wrap the origin and body guards.
    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=resolved.max_request_body_bytes)
    app.add_middleware(OriginCheckMiddleware, allowed_origins=[resolved.frontend_origin])
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)
    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(catalog_router)
    return app


app = create_app()
