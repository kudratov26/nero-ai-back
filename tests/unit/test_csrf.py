"""Origin checks for state-changing requests."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import Scope

from app.api.csrf import OriginCheckMiddleware, normalize_origin, request_origin
from tests.support import TEST_ORIGIN


async def ok(request: Request) -> JSONResponse:
    return JSONResponse({"method": request.method})


def build_app(*, allowed: tuple[str, ...] = (TEST_ORIGIN,)) -> Starlette:
    app = Starlette(
        routes=[
            Route("/thing", ok, methods=["GET", "POST", "PATCH", "DELETE"]),
        ]
    )
    app.add_middleware(OriginCheckMiddleware, allowed_origins=list(allowed))
    return app


async def call(
    app: Starlette,
    method: str = "POST",
    *,
    origin: str | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    merged = dict(headers or {})
    if origin is not None:
        merged["origin"] = origin
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers=merged
    ) as client:
        return await client.request(method, "/thing")


@pytest.mark.parametrize("method", ["POST", "PATCH", "DELETE"])
async def test_matching_origin_is_allowed(method: str) -> None:
    response = await call(build_app(), method, origin=TEST_ORIGIN)

    assert response.status_code == 200


@pytest.mark.parametrize(
    "origin",
    [
        "HTTP://LOCALHOST:3000",
        "http://localhost:3000/",
        "  http://localhost:3000  ",
    ],
)
async def test_origin_comparison_ignores_case_slash_and_padding(origin: str) -> None:
    response = await call(build_app(), origin=origin)

    assert response.status_code == 200


async def test_the_requests_own_origin_is_allowed() -> None:
    response = await call(build_app(), origin="http://test")

    assert response.status_code == 200


async def test_forwarded_headers_describe_the_public_origin() -> None:
    app = build_app()
    response = await call(
        app,
        origin="https://api.example.com",
        headers={"x-forwarded-proto": "https", "x-forwarded-host": "api.example.com"},
    )

    assert response.status_code == 200

    downgraded = await call(
        app,
        origin="http://api.example.com",
        headers={"x-forwarded-proto": "https", "x-forwarded-host": "api.example.com"},
    )
    assert downgraded.status_code == 403


async def test_missing_origin_is_rejected() -> None:
    response = await call(build_app())

    assert response.status_code == 403
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["vary"] == "Origin"
    body = response.json()
    assert body["code"] == "origin_not_allowed"
    assert body["status"] == 403
    assert body["requestId"]


@pytest.mark.parametrize(
    "origin",
    ["https://evil.example", "null", "http://localhost:3001", "http://localhost:3000.evil"],
)
async def test_foreign_origins_are_rejected(origin: str) -> None:
    response = await call(build_app(), origin=origin)

    assert response.status_code == 403
    assert response.json()["code"] == "origin_not_allowed"


async def test_safe_methods_do_not_require_an_origin() -> None:
    response = await call(build_app(), "GET")

    assert response.status_code == 200


async def test_an_allowlisted_origin_does_not_need_a_host_header() -> None:
    scope: dict[str, Any] = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/thing",
        "raw_path": b"/thing",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"origin", TEST_ORIGIN.encode("latin-1"))],
        "server": ("test", 80),
        "client": ("test", 1),
    }

    result = await _drive(build_app(), scope)

    assert result["status"] == 200


async def test_rejection_keeps_an_existing_request_id() -> None:
    scope: dict[str, Any] = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/thing",
        "raw_path": b"/thing",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"test")],
        "server": ("test", 80),
        "client": ("test", 1),
        "state": {"request_id": "abc123"},
    }
    app = build_app()

    response = await _drive(app, scope)

    assert response["body"]["requestId"] == "abc123"


async def _drive(app: Starlette, scope: Scope) -> dict[str, Any]:
    """Run one request and collect the problem body and status."""
    messages: list[dict[str, Any]] = []
    request_messages = [{"type": "http.request", "body": b"", "more_body": False}]

    async def receive() -> dict[str, Any]:
        return request_messages.pop(0) if request_messages else {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(scope, receive, send)
    status = next(
        message["status"] for message in messages if message["type"] == "http.response.start"
    )
    raw = b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    )
    return {"status": status, "body": json.loads(raw) if raw else {}}


def test_normalize_origin() -> None:
    assert normalize_origin(" HTTPS://Example.com:443/ ") == "https://example.com:443"


def test_request_origin_prefers_forwarded_headers() -> None:
    scope: Scope = {
        "type": "http",
        "scheme": "http",
        "headers": [
            (b"host", b"internal:8000"),
            (b"x-forwarded-host", b"api.example.com, proxy.internal"),
            (b"x-forwarded-proto", b"https, http"),
        ],
    }

    assert request_origin(scope) == "https://api.example.com"


def test_request_origin_is_none_without_a_host() -> None:
    assert request_origin({"type": "http", "scheme": "http", "headers": []}) is None
