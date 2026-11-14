"""Health endpoint integration tests over the ASGI transport."""

from __future__ import annotations

import httpx
import pytest

from app.main import create_app
from tests.support import asgi_client, make_settings


@pytest.fixture
async def client() -> httpx.AsyncClient:
    app = create_app(make_settings())
    async with asgi_client(app) as http:
        yield http


async def test_live_returns_ok(client: httpx.AsyncClient) -> None:
    response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["content-type"] == "application/json"


async def test_live_sets_request_id(client: httpx.AsyncClient) -> None:
    response = await client.get("/health/live")

    assert len(response.headers["x-request-id"]) == 32


async def test_openapi_exposes_only_documented_routes(
    client: httpx.AsyncClient,
) -> None:
    schema = (await client.get("/openapi.json")).json()

    assert set(schema["paths"]) == {"/health/live", "/health/ready"}
    assert schema["info"]["title"] == "Nero AI API"
