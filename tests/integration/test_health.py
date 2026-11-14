"""Health endpoint integration tests over the ASGI transport."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import Settings
from app.main import create_app
from tests.conftest import TEST_DATABASE_URL


@pytest.fixture
def app() -> FastAPI:
    return create_app(
        Settings(
            _env_file=None,
            database_url=TEST_DATABASE_URL,
            app_secret="test-secret",
        )
    )


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


async def test_live_returns_ok(client: httpx.AsyncClient) -> None:
    response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["content-type"] == "application/json"


async def test_openapi_exposes_only_documented_routes(
    client: httpx.AsyncClient,
) -> None:
    schema = (await client.get("/openapi.json")).json()

    assert set(schema["paths"]) == {"/health/live"}
    assert schema["info"]["title"] == "Nero AI API"
