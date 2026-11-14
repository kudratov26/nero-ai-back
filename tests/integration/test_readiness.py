"""Readiness probe behaviour against a real and unreachable database."""

from __future__ import annotations

from app.main import create_app
from tests.support import asgi_client, make_settings


async def test_ready_when_database_is_migrated(
    migrated_database: str,
) -> None:
    async with asgi_client(create_app(make_settings())) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    assert response.headers["x-request-id"]


async def test_not_ready_when_database_is_unreachable() -> None:
    settings = make_settings(database_url="postgresql+asyncpg://nero:nero@127.0.0.1:1/nero_test")
    async with asgi_client(create_app(settings)) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["code"] == "internal_error"
    assert body["detail"] == "The service is not ready."
    assert "127.0.0.1" not in response.text
