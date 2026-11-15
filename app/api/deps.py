"""Shared FastAPI dependencies."""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.domain.auth import AuthContext, authenticate


def get_app_settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise RuntimeError("the session factory is not initialized")
    return cast(async_sessionmaker[AsyncSession], factory)


async def get_current_auth(
    request: Request,
    settings: Annotated[Settings, Depends(get_app_settings)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> AuthContext:
    token = request.cookies.get(settings.session_cookie_name)
    return await authenticate(session_factory, token=token)
