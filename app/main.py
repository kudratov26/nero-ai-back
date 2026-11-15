"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.auth import router as auth_router
from app.api.body_limit import RequestBodyLimitMiddleware
from app.api.catalog import router as catalog_router
from app.api.chats import router as chats_router
from app.api.credits import router as credits_router
from app.api.csrf import OriginCheckMiddleware
from app.api.health import router as health_router
from app.api.problems import register_exception_handlers
from app.api.request_context import RequestContextMiddleware
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.core.rate_limit import MemoryRateLimiter
from app.db.session import create_engine, create_session_factory
from app.providers.base import ChatProvider
from app.providers.openai_compatible import build_chat_provider


def create_app(
    settings: Settings | None = None,
    *,
    chat_provider: ChatProvider | None = None,
) -> FastAPI:
    """Build the ASGI app. Takes explicit settings in tests.

    `chat_provider` is a test seam: the fake provider is injected instead of
    building an HTTP adapter from settings. Production passes nothing and the
    lifespan builds the configured adapter (or `None` when unconfigured).
    """
    resolved = settings if settings is not None else get_settings()
    configure_logging(resolved)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(resolved.database_url)
        app.state.engine = engine
        app.state.session_factory = create_session_factory(engine)
        provider = chat_provider if chat_provider is not None else build_chat_provider(resolved)
        app.state.chat_provider = provider
        try:
            yield
        finally:
            try:
                if provider is not None:
                    await provider.aclose()
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
    app.include_router(credits_router)
    app.include_router(chats_router)
    return app


app = create_app()
