"""RFC 9457 problem details across validation, HTTP, domain, and unhandled errors."""

from __future__ import annotations

import httpx
import pytest

from tests.support import asgi_client, build_app

PROBLEM_KEYS = {"type", "title", "status", "code", "detail", "requestId"}


@pytest.fixture
async def client() -> httpx.AsyncClient:
    async with asgi_client(build_app()) as http:
        yield http


async def test_unknown_route_returns_problem(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/missing")

    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert set(body) == PROBLEM_KEYS
    assert body["type"] == "https://nero.ai/problems/not-found"
    assert body["title"] == "Not Found"
    assert body["status"] == 404
    assert body["code"] == "not_found"


async def test_method_not_allowed_returns_problem(client: httpx.AsyncClient) -> None:
    response = await client.delete("/health/live")

    assert response.status_code == 405
    assert response.json()["code"] == "invalid_request"


async def test_validation_error_does_not_echo_values(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/_test/echo",
        json={"email": "creator@example.com", "password": "hunter2"},
    )

    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "invalid_request"
    assert "password" in body["detail"]
    assert "hunter2" not in response.text


async def test_validation_error_lists_missing_fields(client: httpx.AsyncClient) -> None:
    response = await client.post("/_test/echo", json={"email": "creator@example.com"})

    body = response.json()
    assert body["status"] == 400
    assert "password" in body["detail"]
    assert "Field required" in body["detail"]


async def test_domain_error_keeps_code_and_detail(client: httpx.AsyncClient) -> None:
    response = await client.get("/_test/credits")

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "insufficient_credits"
    assert body["title"] == "Insufficient Credits"
    assert body["detail"] == "This run needs 15 Credits; 6 are available."
    assert body["type"] == "https://nero.ai/problems/insufficient-credits"


async def test_unhandled_error_is_generic(client: httpx.AsyncClient) -> None:
    response = await client.get("/_test/boom")

    assert response.status_code == 500
    body = response.json()
    assert body["code"] == "internal_error"
    assert body["detail"] == "An unexpected error occurred."
    assert "boom-secret-prompt" not in response.text
    assert "Traceback" not in response.text
    assert body["requestId"] == response.headers["x-request-id"]


async def test_unknown_route_problem_carries_request_id(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/v1/missing")

    assert response.json()["requestId"] == response.headers["x-request-id"]
