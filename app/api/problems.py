"""RFC 9457 problem details for every error response.

The client maps `code` values to copy. Responses never include provider bodies,
SQL, stack traces, or validation input values.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from enum import StrEnum
from http import HTTPStatus
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

PROBLEM_BASE_URL = "https://nero.ai/problems/"
PROBLEM_MEDIA_TYPE = "application/problem+json"
REQUEST_ID_HEADER = "X-Request-ID"
GENERIC_INTERNAL_DETAIL = "An unexpected error occurred."
_MAX_VALIDATION_ERRORS = 10


class ErrorCode(StrEnum):
    """Stable machine-readable error codes from `docs/SPEC.md` section 9."""

    INVALID_REQUEST = "invalid_request"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    DUPLICATE_MESSAGE = "duplicate_message"
    EMAIL_TAKEN = "email_taken"
    INVALID_CREDENTIALS = "invalid_credentials"
    WEAK_PASSWORD = "weak_password"
    RATE_LIMITED = "rate_limited"
    INSUFFICIENT_CREDITS = "insufficient_credits"
    MODEL_UNAVAILABLE = "model_unavailable"
    PROVIDER_ERROR = "provider_error"
    PROVIDER_TIMEOUT = "provider_timeout"
    GENERATION_NOT_CANCELLABLE = "generation_not_cancellable"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    ORIGIN_NOT_ALLOWED = "origin_not_allowed"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    INTERNAL_ERROR = "internal_error"


class ProblemDetail(BaseModel):
    type: str
    title: str
    status: int
    code: ErrorCode
    detail: str
    request_id: str = Field(serialization_alias="requestId")


class ApiError(Exception):
    """Raise from services and routers to return a documented problem."""

    def __init__(
        self,
        code: ErrorCode,
        status: int,
        *,
        detail: str | None = None,
        title: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.code = code
        self.status = status
        self.title = title or default_title(code)
        self.detail = detail or self.title
        self.headers = dict(headers) if headers else None
        super().__init__(self.detail)


def default_title(code: ErrorCode) -> str:
    return code.value.replace("_", " ").title()


def problem_response(
    *,
    status: int,
    code: ErrorCode,
    detail: str,
    request_id: str,
    title: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    body = ProblemDetail(
        type=f"{PROBLEM_BASE_URL}{code.value.replace('_', '-')}",
        title=title or default_title(code),
        status=status,
        code=code,
        detail=detail,
        request_id=request_id,
    )
    response_headers = {REQUEST_ID_HEADER: request_id}
    if headers:
        response_headers.update(headers)
    return JSONResponse(
        content=body.model_dump(mode="json", by_alias=True),
        status_code=status,
        media_type=PROBLEM_MEDIA_TYPE,
        headers=response_headers,
    )


def get_request_id(request: Request) -> str:
    """Return the middleware-assigned request id, or a fresh one outside HTTP."""
    request_id = getattr(request.state, "request_id", None)
    if isinstance(request_id, str) and request_id:
        return request_id
    return uuid.uuid4().hex


_HTTP_ERROR_CODES: dict[int, ErrorCode] = {
    400: ErrorCode.INVALID_REQUEST,
    401: ErrorCode.UNAUTHORIZED,
    403: ErrorCode.FORBIDDEN,
    404: ErrorCode.NOT_FOUND,
    405: ErrorCode.INVALID_REQUEST,
    409: ErrorCode.CONFLICT,
    413: ErrorCode.PAYLOAD_TOO_LARGE,
    415: ErrorCode.INVALID_REQUEST,
    422: ErrorCode.INVALID_REQUEST,
    429: ErrorCode.RATE_LIMITED,
    500: ErrorCode.INTERNAL_ERROR,
}


def _code_for_status(status: int) -> ErrorCode:
    if status in _HTTP_ERROR_CODES:
        return _HTTP_ERROR_CODES[status]
    if status >= 500:
        return ErrorCode.INTERNAL_ERROR
    return ErrorCode.INVALID_REQUEST


def _status_phrase(status: int) -> str:
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return default_title(_code_for_status(status))


def _validation_detail(errors: Sequence[Mapping[str, Any]]) -> str:
    # Field locations and messages only: never echo submitted values.
    prefixes = {"body", "query", "path", "header", "cookie"}
    parts: list[str] = []
    for error in errors[:_MAX_VALIDATION_ERRORS]:
        location = [str(part) for part in error.get("loc", ()) if str(part) not in prefixes]
        field = ".".join(location) or "request"
        parts.append(f"{field}: {error.get('msg', 'invalid value')}")
    if len(errors) > _MAX_VALIDATION_ERRORS:
        parts.append(f"and {len(errors) - _MAX_VALIDATION_ERRORS} more")
    return "; ".join(parts) or "The request is invalid."


async def api_error_handler(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, ApiError):
        return await unhandled_exception_handler(request, exc)
    return problem_response(
        status=exc.status,
        code=exc.code,
        detail=exc.detail,
        title=exc.title,
        request_id=get_request_id(request),
        headers=exc.headers,
    )


async def http_exception_handler(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, StarletteHTTPException):
        return await unhandled_exception_handler(request, exc)
    status = exc.status_code
    if status >= 500:
        detail = GENERIC_INTERNAL_DETAIL
    elif isinstance(exc.detail, str) and exc.detail:
        detail = exc.detail
    else:
        detail = _status_phrase(status)
    return problem_response(
        status=status,
        code=_code_for_status(status),
        detail=detail,
        request_id=get_request_id(request),
        headers=exc.headers,
    )


async def validation_exception_handler(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, RequestValidationError):
        return await unhandled_exception_handler(request, exc)
    return problem_response(
        status=400,
        code=ErrorCode.INVALID_REQUEST,
        detail=_validation_detail(exc.errors()),
        request_id=get_request_id(request),
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> Response:
    structlog.get_logger("app.error").exception(
        "unhandled_error",
        method=request.method,
        path=request.url.path,
    )
    return problem_response(
        status=500,
        code=ErrorCode.INTERNAL_ERROR,
        detail=GENERIC_INTERNAL_DETAIL,
        request_id=get_request_id(request),
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
