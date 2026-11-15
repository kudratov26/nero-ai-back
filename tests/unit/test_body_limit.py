"""Request body size cap."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.api.body_limit import RequestBodyLimitMiddleware


def build_app(*, max_bytes: int) -> Starlette:
    async def echo(request: Request) -> JSONResponse:
        body = await request.body()
        return JSONResponse({"length": len(body)})

    app = Starlette(routes=[Route("/echo", echo, methods=["POST"])])
    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=max_bytes)
    return app


async def post(app: Starlette, content: bytes | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/echo", content=content)


async def test_a_body_within_the_limit_is_processed() -> None:
    response = await post(build_app(max_bytes=64), b"x" * 64)

    assert response.status_code == 200
    assert response.json() == {"length": 64}


async def test_a_declared_body_over_the_limit_is_rejected() -> None:
    response = await post(build_app(max_bytes=64), b"x" * 65)

    assert response.status_code == 413
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "payload_too_large"


async def test_a_streamed_body_over_the_limit_is_rejected() -> None:
    app = build_app(max_bytes=64)
    transport = httpx.ASGITransport(app=app)

    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(3):
            yield b"x" * 32

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/echo", content=chunks())

    assert response.status_code == 413
    assert response.json()["code"] == "payload_too_large"


async def test_the_handler_does_not_run_for_a_rejected_body() -> None:
    calls = 0

    async def handler(request: Request) -> JSONResponse:
        nonlocal calls
        calls += 1
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/echo", handler, methods=["POST"])])
    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=8)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/echo", content=b"way too long")

    assert response.status_code == 413
    assert calls == 0


async def test_non_http_scopes_pass_through() -> None:
    seen: list[str] = []

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        seen.append(scope["type"])

    async def unused(message: Any) -> None:
        raise AssertionError("the middleware must not touch a non-HTTP scope")

    middleware = RequestBodyLimitMiddleware(inner, max_bytes=1)

    await middleware({"type": "lifespan"}, unused, unused)

    assert seen == ["lifespan"]


async def test_safe_methods_are_not_limited() -> None:
    async def handler(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/echo", handler, methods=["GET"])])
    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=1)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/echo")

    assert response.status_code == 200


async def test_a_malformed_content_length_falls_back_to_counting() -> None:
    scope: dict[str, Any] = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/echo",
        "raw_path": b"/echo",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"test"), (b"content-length", b"not-a-number")],
        "server": ("test", 80),
        "client": ("test", 1),
    }

    async def handler(request: Request) -> JSONResponse:
        body = await request.body()
        return JSONResponse({"length": len(body)})

    app = Starlette(routes=[Route("/echo", handler, methods=["POST"])])
    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=4)

    messages: list[dict[str, Any]] = []
    request_messages = [{"type": "http.request", "body": b"x" * 5, "more_body": False}]

    async def receive() -> dict[str, Any]:
        return request_messages.pop(0) if request_messages else {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(scope, receive, send)

    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    assert status == 413
