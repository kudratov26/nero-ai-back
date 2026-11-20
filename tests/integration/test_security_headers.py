"""Baseline security headers on every response.

The middleware is outermost, so success, validation, auth, origin, body-limit,
and crash responses all carry the same headers. HSTS is production-only: it
would pin `http://localhost` for a year otherwise.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI, Response

from app.api.security_headers import DENY, HSTS, NO_REFERRER, NOSNIFF
from app.core.config import DEFAULT_MAX_BODY_BYTES, Settings
from tests.support import asgi_client, build_app, make_settings

BASELINE = {
    "x-content-type-options": NOSNIFF,
    "referrer-policy": NO_REFERRER,
    "x-frame-options": DENY,
}


def assert_baseline(response: httpx.Response) -> None:
    for name, value in BASELINE.items():
        assert response.headers[name] == value


async def test_a_success_response_carries_the_headers() -> None:
    async with asgi_client(build_app()) as http:
        response = await http.get("/health/live")

    assert response.status_code == 200
    assert_baseline(response)
    assert "strict-transport-security" not in response.headers


async def test_a_not_found_response_carries_the_headers() -> None:
    async with asgi_client(build_app()) as http:
        response = await http.get("/v1/does-not-exist")

    assert response.status_code in {401, 404}
    assert_baseline(response)


async def test_a_validation_error_carries_the_headers() -> None:
    async with asgi_client(build_app()) as http:
        response = await http.post("/_test/echo", json={"email": "not-an-email"})

    assert response.status_code == 400
    assert_baseline(response)


async def test_a_missing_origin_rejection_carries_the_headers() -> None:
    async with asgi_client(build_app(), origin=None) as http:
        response = await http.post(
            "/_test/echo",
            json={"email": "creator@example.com", "password": "correct horse battery"},
        )

    assert response.status_code == 403
    assert_baseline(response)


async def test_an_oversized_body_carries_the_headers() -> None:
    async with asgi_client(build_app()) as http:
        response = await http.post(
            "/_test/echo",
            content=b"x" * (DEFAULT_MAX_BODY_BYTES + 1),
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 413
    assert_baseline(response)


async def test_a_crash_response_carries_the_headers() -> None:
    async with asgi_client(build_app()) as http:
        response = await http.get("/_test/boom")

    assert response.status_code == 500
    assert_baseline(response)


async def test_hsts_is_sent_in_production() -> None:
    settings = make_settings(
        app_env="production",
        app_secret="production-secret-not-in-repo",
        rate_limit_backend="memory",
    )

    async with asgi_client(build_app_for(settings)) as http:
        response = await http.get("/health/live")

    assert response.status_code == 200
    assert response.headers["strict-transport-security"] == HSTS


def build_app_for(settings: Settings) -> FastAPI:
    """Create the real app with explicit settings (production HSTS case)."""
    from app.main import create_app

    return create_app(settings)


async def test_a_route_may_override_a_baseline_header() -> None:
    app = build_app()

    @app.get("/_test/frame")
    async def frame(response: Response) -> dict[str, bool]:
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        return {"ok": True}

    async with asgi_client(app) as http:
        response = await http.get("/_test/frame")

    assert response.status_code == 200
    assert response.headers["x-frame-options"] == "SAMEORIGIN"
    assert response.headers["x-content-type-options"] == NOSNIFF


@pytest.mark.parametrize("path", ["/health/live", "/health/ready"])
async def test_health_routes_carry_the_headers(path: str, migrated_database: str) -> None:
    async with asgi_client(build_app()) as http:
        response = await http.get(path)

    assert_baseline(response)
