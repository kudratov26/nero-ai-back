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
TEST_ORIGIN = "http://localhost:3000"
PASSWORD = "correct horse battery"


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


def assert_problem(response: httpx.Response, status: int, code: str) -> dict[str, Any]:
    """Assert the RFC 9457 shape and return the parsed body."""
    assert response.status_code == status
    assert response.headers["content-type"] == "application/problem+json"
    body: dict[str, Any] = response.json()
    assert body["status"] == status
    assert body["code"] == code
    assert body["requestId"]
    return body


async def post_signup(
    client: httpx.AsyncClient,
    email: str,
    *,
    password: str = PASSWORD,
    display_name: str | None = "Creator",
) -> httpx.Response:
    payload: dict[str, str] = {"email": email, "password": password}
    if display_name is not None:
        payload["displayName"] = display_name
    return await client.post("/v1/auth/signup", json=payload)


@asynccontextmanager
async def asgi_client(
    app: FastAPI,
    *,
    origin: str | None = TEST_ORIGIN,
    headers: dict[str, str] | None = None,
) -> AsyncIterator[httpx.AsyncClient]:
    """HTTP client that runs the app's lifespan (engine setup and disposal).

    Sends the allowlisted `Origin` by default because unsafe methods require it;
    pass `origin=None` to exercise the missing-header path.
    """
    merged = dict(headers or {})
    if origin is not None:
        merged.setdefault("origin", origin)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test", headers=merged) as client,
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
