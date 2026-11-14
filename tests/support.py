"""Shared fixtures and helpers for tests."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI
from pydantic import BaseModel, Field

from app.api.problems import ApiError, ErrorCode
from app.core.config import Settings

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://nero:nero@localhost:5432/nero_test",
)
TEST_SECRET = "test-secret-not-for-production"


def make_settings(**overrides: Any) -> Settings:
    """Build settings without reading `.env`, so tests stay hermetic."""
    values: dict[str, Any] = {
        "app_env": "test",
        "app_secret": TEST_SECRET,
        "database_url": TEST_DATABASE_URL,
        "log_level": "info",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@asynccontextmanager
async def asgi_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """HTTP client that runs the app's lifespan (engine setup and disposal)."""
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
    ):
        yield client


class EchoBody(BaseModel):
    email: str
    password: str = Field(min_length=10)


def build_app() -> FastAPI:
    """App with routes that exercise each error path."""
    # Imported lazily: `app.main` builds settings at import time, and conftest must
    # set the test database before that happens.
    from app.main import create_app

    app = create_app(make_settings())

    @app.post("/_test/echo")
    async def echo(body: EchoBody) -> dict[str, str]:
        return {"email": body.email}

    @app.get("/_test/boom")
    async def boom() -> None:
        raise RuntimeError("boom-secret-prompt")

    @app.get("/_test/credits")
    async def credits() -> None:
        raise ApiError(
            ErrorCode.INSUFFICIENT_CREDITS,
            409,
            detail="This run needs 15 Credits; 6 are available.",
        )

    return app
