"""Shared FastAPI dependencies."""

from __future__ import annotations

from typing import Annotated, cast

import structlog
from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.problems import ApiError, ErrorCode
from app.core.config import Settings
from app.core.rate_limit import RateLimiter
from app.domain.auth import AuthContext, authenticate
from app.providers.base import ChatProvider
from app.storage.base import Storage


def get_app_settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise RuntimeError("the session factory is not initialized")
    return cast(async_sessionmaker[AsyncSession], factory)


def get_rate_limiter(request: Request) -> RateLimiter:
    limiter = getattr(request.app.state, "rate_limiter", None)
    if limiter is None:
        raise RuntimeError("the rate limiter is not initialized")
    return cast(RateLimiter, limiter)


def get_chat_provider(request: Request) -> ChatProvider | None:
    """Return the configured chat provider, or `None` when the gateway is unset."""
    provider = getattr(request.app.state, "chat_provider", None)
    return cast(ChatProvider | None, provider)


def get_storage(request: Request) -> Storage:
    """Return the app-wide storage adapter built by the lifespan."""
    storage = getattr(request.app.state, "storage", None)
    if storage is None:
        raise RuntimeError("the storage adapter is not initialized")
    return cast(Storage, storage)


async def enforce_rate_limit(
    limiter: RateLimiter,
    key: str,
    *,
    limit: int,
    window_seconds: int,
) -> None:
    """Record one attempt against a budget, raising 429 when it is exhausted."""
    retry_after = await limiter.hit(key, limit=limit, window_seconds=window_seconds)
    if retry_after is None:
        return
    raise ApiError(
        ErrorCode.RATE_LIMITED,
        429,
        detail="Too many attempts. Try again later.",
        headers={"Retry-After": str(retry_after)},
    )


async def get_current_auth(
    request: Request,
    settings: Annotated[Settings, Depends(get_app_settings)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> AuthContext:
    token = request.cookies.get(settings.session_cookie_name)
    context = await authenticate(session_factory, token=token)
    # Available to every log emitted after the dependency, including the request
    # completion event; cleared by the request middleware.
    structlog.contextvars.bind_contextvars(userId=str(context.user.id))
    return context


async def get_optional_auth(
    request: Request,
    settings: Annotated[Settings, Depends(get_app_settings)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> AuthContext | None:
    """Resolve a session when one is present, without rejecting anonymous callers.

    Only routes that must answer to signed-out readers use this. A bad or expired
    cookie is treated as anonymous rather than `401`, because the asset route
    reports a denied read as `404` and must not reveal session state.
    """
    token = request.cookies.get(settings.session_cookie_name)
    if not token:
        return None
    try:
        context = await authenticate(session_factory, token=token)
    except ApiError as exc:
        if exc.code is ErrorCode.UNAUTHORIZED:
            return None
        raise
    structlog.contextvars.bind_contextvars(userId=str(context.user.id))
    return context
