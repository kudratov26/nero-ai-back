"""Shared fixtures and helpers for tests."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, NamedTuple

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


class RouteCase(NamedTuple):
    """One HTTP method on one documented path.

    `anonymous_status` is the outcome an unauthenticated caller gets, so the
    authorization matrix has an explicit expectation per route rather than an
    inferred one. `None` means "any response that is not the `401 unauthorized`
    problem", for routes whose exact status depends on seeded data.

    `probe_body` overrides the empty `{}` sent for methods that carry a body. An
    empty object reaches the auth dependency but not the handler, so a route that
    validates its body first would answer `400` before the ownership check runs.
    """

    method: str
    path: str
    anonymous_status: int | None
    anonymous_code: str | None = None
    probe_body: dict[str, object] | None = None


# Every route the API serves, split by whether it needs a session. The inventory
# test in `tests/unit/test_route_matrix.py` fails when the app exposes a route
# missing from these tables, so a new endpoint cannot ship without an explicit
# authentication decision and, when protected, the `get_current_auth` dependency.
#
# FastAPI's own `/openapi.json`, `/docs`, and `/docs/oauth2-redirect` are excluded:
# they are framework plumbing, not part of the contract.
PUBLIC_ROUTES: tuple[RouteCase, ...] = (
    RouteCase("GET", "/health/live", 200),
    RouteCase("GET", "/health/ready", 200),
    RouteCase("GET", "/v1/plans", 200),
    RouteCase("GET", "/v1/models", 200),
    # The status depends on whether the catalog has been seeded, so only
    # "not rejected as unauthenticated" is asserted.
    RouteCase("GET", "/v1/models/{model_id}", None),
    RouteCase("POST", "/v1/auth/logout", 204),
    RouteCase("POST", "/v1/auth/signup", 400, "invalid_request"),
    RouteCase("POST", "/v1/auth/login", 400, "invalid_request"),
)

PROTECTED_ROUTES: tuple[RouteCase, ...] = (
    RouteCase("GET", "/v1/auth/me", 401, "unauthorized"),
    RouteCase("PATCH", "/v1/auth/me", 401, "unauthorized"),
    RouteCase("POST", "/v1/auth/password", 401, "unauthorized"),
    RouteCase("GET", "/v1/auth/sessions", 401, "unauthorized"),
    RouteCase("DELETE", "/v1/auth/sessions/{session_id}", 401, "unauthorized"),
    RouteCase("GET", "/v1/credits", 401, "unauthorized"),
    RouteCase("GET", "/v1/credits/ledger", 401, "unauthorized"),
    RouteCase("POST", "/v1/account/plan", 401, "unauthorized"),
    RouteCase("GET", "/v1/chats", 401, "unauthorized"),
    RouteCase("POST", "/v1/chats", 401, "unauthorized"),
    RouteCase("GET", "/v1/chats/{chat_id}", 401, "unauthorized"),
    # A rename probe needs a valid title, or the response is a 400 from body
    # validation instead of the 401 from the auth dependency this table asserts.
    RouteCase(
        "PATCH",
        "/v1/chats/{chat_id}",
        401,
        "unauthorized",
        probe_body={"title": "Probe"},
    ),
    RouteCase("DELETE", "/v1/chats/{chat_id}", 401, "unauthorized"),
)

ROUTES: tuple[RouteCase, ...] = PUBLIC_ROUTES + PROTECTED_ROUTES
DOCUMENTED_PATHS: frozenset[str] = frozenset(case.path for case in ROUTES)

#: Sample values for `{param}` placeholders, so probes reach the handler with a
#: syntactically valid path instead of failing validation.
PATH_PARAM_SAMPLES: dict[str, str] = {
    "session_id": "0193a0e0-0000-7000-8000-000000000000",
    "model_id": "model-does-not-exist",
    "chat_id": "0193a0e0-0000-7000-8000-000000000000",
}


def route_path(case: RouteCase) -> str:
    """Fill a route template's path parameters with sample values."""
    path = case.path
    for name, value in PATH_PARAM_SAMPLES.items():
        path = path.replace(f"{{{name}}}", value)
    return path


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


def is_unauthorized(response: httpx.Response) -> bool:
    """Whether the response is the `401 unauthorized` problem."""
    if response.status_code != 401:
        return False
    if not response.headers.get("content-type", "").startswith("application/problem+json"):
        return False
    return bool(response.json().get("code") == "unauthorized")


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
