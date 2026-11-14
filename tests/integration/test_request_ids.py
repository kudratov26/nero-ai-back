"""X-Request-ID handling: propagation, generation, and sanitizing."""

from __future__ import annotations

import re

import httpx
import pytest

from tests.support import asgi_client, build_app

GENERATED_ID = re.compile(r"^[0-9a-f]{32}$")


@pytest.fixture
async def client() -> httpx.AsyncClient:
    async with asgi_client(build_app()) as http:
        yield http


async def test_client_request_id_is_echoed(client: httpx.AsyncClient) -> None:
    response = await client.get("/health/live", headers={"X-Request-ID": "req-abc.123"})

    assert response.headers["x-request-id"] == "req-abc.123"


async def test_inbound_request_id_reaches_problem_body(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/v1/missing", headers={"X-Request-ID": "req-404"})

    assert response.json()["requestId"] == "req-404"


async def test_missing_request_id_is_generated(client: httpx.AsyncClient) -> None:
    first = await client.get("/health/live")
    second = await client.get("/health/live")

    assert GENERATED_ID.fullmatch(first.headers["x-request-id"])
    assert GENERATED_ID.fullmatch(second.headers["x-request-id"])
    assert first.headers["x-request-id"] != second.headers["x-request-id"]


@pytest.mark.parametrize(
    "inbound",
    [
        "contains spaces and is unsafe",
        "a" * 200,
        "",
    ],
)
async def test_unsafe_request_id_is_replaced(client: httpx.AsyncClient, inbound: str) -> None:
    response = await client.get("/health/live", headers={"X-Request-ID": inbound})

    returned = response.headers["x-request-id"]
    assert returned != inbound
    assert GENERATED_ID.fullmatch(returned)


async def test_request_id_on_unhandled_error(client: httpx.AsyncClient) -> None:
    response = await client.get("/_test/boom", headers={"X-Request-ID": "req-500"})

    assert response.status_code == 500
    assert response.headers["x-request-id"] == "req-500"
    assert response.json()["requestId"] == "req-500"
