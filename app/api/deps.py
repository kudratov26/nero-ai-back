"""Shared FastAPI dependencies."""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.problems import ApiError, ErrorCode
from app.core.config import Settings
from app.core.rate_limit import MemoryRateLimiter
from app.domain.auth import AuthContext, authenticate


def get_app_settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise RuntimeError("the session factory is not initialized")
    return cast(async_sessionmaker[AsyncSession], factory)


def get_rate_limiter(request: Request) -> MemoryRateLimiter:
    limiter = getattr(request.app.state, "rate_limiter", None)
    if limiter is None:
        raise RuntimeError("the rate limiter is not initialized")
    return cast(MemoryRateLimiter, limiter)


def enforce_rate_limit(
    limiter: MemoryRateLimiter,
    key: str,
    *,
    limit: int,
    window_seconds: int,
) -> None:
    """Record one attempt against a budget, raising 429 when it is exhausted."""
    retry_after = limiter.hit(key, limit=limit, window_seconds=window_seconds)
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
    return await authenticate(session_factory, token=token)
