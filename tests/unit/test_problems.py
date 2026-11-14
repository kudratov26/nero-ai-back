"""Direct handler tests for branches the HTTP-level tests do not reach."""

from __future__ import annotations

import json
from typing import Any

from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import Response

from app.api.problems import (
    GENERIC_INTERNAL_DETAIL,
    PROBLEM_MEDIA_TYPE,
    ApiError,
    ErrorCode,
    _validation_detail,
    api_error_handler,
    get_request_id,
    http_exception_handler,
    problem_response,
    validation_exception_handler,
)


def make_request(path: str = "/x") -> Request:
    scope: dict[str, Any] = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "server": ("test", 80),
        "client": ("test", 1),
    }
    return Request(scope)


def body_of(response: Response) -> dict[str, Any]:
    return json.loads(response.body)


def test_get_request_id_falls_back_outside_middleware() -> None:
    request_id = get_request_id(make_request())

    assert len(request_id) == 32
    assert get_request_id(make_request()) != request_id


def test_problem_response_serializes_camel_case() -> None:
    response = problem_response(
        status=403,
        code=ErrorCode.ORIGIN_NOT_ALLOWED,
        detail="Origin not allowed.",
        request_id="req-1",
    )

    assert response.status_code == 403
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    assert response.headers["x-request-id"] == "req-1"
    body = body_of(response)
    assert body["requestId"] == "req-1"
    assert body["type"] == "https://nero.ai/problems/origin-not-allowed"
    assert "request_id" not in body


async def test_http_exception_without_detail_uses_status_phrase() -> None:
    response = await http_exception_handler(make_request(), StarletteHTTPException(status_code=405))

    assert body_of(response)["detail"] == "Method Not Allowed"


async def test_http_exception_with_nonstandard_status_is_mapped() -> None:
    response = await http_exception_handler(make_request(), StarletteHTTPException(status_code=499))

    body = body_of(response)
    assert body["status"] == 499
    assert body["code"] == "invalid_request"
    assert body["detail"] == "Invalid Request"


async def test_http_exception_server_error_hides_detail() -> None:
    response = await http_exception_handler(
        make_request(), StarletteHTTPException(status_code=503, detail="upstream down")
    )

    body = body_of(response)
    assert body["code"] == "internal_error"
    assert body["detail"] == GENERIC_INTERNAL_DETAIL


async def test_http_exception_forwards_headers() -> None:
    response = await http_exception_handler(
        make_request(),
        StarletteHTTPException(status_code=429, headers={"Retry-After": "60"}),
    )

    assert response.headers["retry-after"] == "60"
    assert body_of(response)["code"] == "rate_limited"


async def test_api_error_forwards_headers() -> None:
    error = ApiError(
        ErrorCode.RATE_LIMITED,
        429,
        detail="Slow down.",
        headers={"Retry-After": "30"},
    )
    response = await api_error_handler(make_request(), error)

    assert response.headers["retry-after"] == "30"
    assert body_of(response)["title"] == "Rate Limited"


async def test_api_error_handler_falls_back_for_foreign_exceptions() -> None:
    response = await api_error_handler(make_request(), RuntimeError("boom"))

    body = body_of(response)
    assert response.status_code == 500
    assert body["code"] == "internal_error"
    assert "boom" not in body["detail"]


async def test_http_exception_handler_falls_back_for_foreign_exceptions() -> None:
    response = await http_exception_handler(make_request(), RuntimeError("boom"))

    assert body_of(response)["code"] == "internal_error"


async def test_validation_handler_falls_back_for_foreign_exceptions() -> None:
    response = await validation_exception_handler(make_request(), RuntimeError("boom"))

    assert body_of(response)["code"] == "internal_error"


def test_validation_detail_is_capped() -> None:
    errors: list[dict[str, Any]] = [
        {"loc": ("body", "items", str(index)), "msg": "Field required"} for index in range(12)
    ]

    detail = _validation_detail(errors)

    assert "and 2 more" in detail
    assert detail.count("Field required") == 10


def test_validation_detail_never_includes_input() -> None:
    error = {
        "loc": ("body", "password"),
        "msg": "String should have at least 10 characters",
        "input": "hunter2",
    }

    detail = _validation_detail([error])

    assert "password" in detail
    assert "hunter2" not in detail


async def test_validation_handler_returns_400() -> None:
    request = make_request()
    error = RequestValidationError(
        [{"loc": ("body", "email"), "msg": "Field required", "type": "missing"}]
    )

    response = await validation_exception_handler(request, error)

    assert response.status_code == 400
    assert body_of(response)["code"] == "invalid_request"
    assert len(get_request_id(request)) == 32
