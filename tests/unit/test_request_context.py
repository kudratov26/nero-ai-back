"""Middleware edge cases that the HTTP-level tests cannot reach."""

from __future__ import annotations

from typing import Any

import pytest

from app.api.request_context import RequestContextMiddleware, _outcome


def test_outcome_classification() -> None:
    assert _outcome(200) == "success"
    assert _outcome(404) == "client_error"
    assert _outcome(502) == "error"


def http_scope() -> dict[str, Any]:
    return {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/x",
        "raw_path": b"/x",
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "server": ("test", 80),
        "client": ("test", 1),
        "state": {},
    }


async def no_receive() -> dict[str, Any]:
    return {"type": "http.request"}


async def test_non_http_scope_passes_through() -> None:
    seen: list[str] = []

    async def app(scope, receive, send):
        seen.append(scope["type"])

    middleware = RequestContextMiddleware(app)
    await middleware({"type": "lifespan"}, no_receive, lambda message: None)

    assert seen == ["lifespan"]


async def test_exception_after_response_start_is_reraised() -> None:
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise RuntimeError("stream broke")

    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    middleware = RequestContextMiddleware(app)
    with pytest.raises(RuntimeError):
        await middleware(http_scope(), no_receive, send)

    assert sent[0]["type"] == "http.response.start"
