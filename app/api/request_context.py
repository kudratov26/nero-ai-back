"""Request IDs, access logs, and the catch-all 500 response.

Implemented as pure ASGI middleware so it never buffers streaming responses.
"""

from __future__ import annotations

import re
import time
import uuid
from typing import Any

import structlog
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.problems import (
    GENERIC_INTERNAL_DETAIL,
    REQUEST_ID_HEADER,
    ErrorCode,
    problem_response,
)
from app.core.metrics import METRICS

_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _incoming_request_id(scope: Scope) -> str | None:
    """Accept a client request id only if it is short and log-safe."""
    for name, value in scope.get("headers", []):
        if name == REQUEST_ID_HEADER.lower().encode("latin-1"):
            candidate: str = bytes(value).decode("latin-1").strip()
            if _REQUEST_ID_PATTERN.fullmatch(candidate):
                return candidate
            return None
    return None


def _duration_ms(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)


def _outcome(status_code: int) -> str:
    if status_code < 400:
        return "success"
    if status_code < 500:
        return "client_error"
    return "error"


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _incoming_request_id(scope) or uuid.uuid4().hex
        state: dict[str, Any] = scope.setdefault("state", {})
        state["request_id"] = request_id

        method = scope["method"]
        path = scope["path"]
        started = time.perf_counter()
        status_code = 500
        response_started = False
        logger = structlog.get_logger("app.request")

        async def send_with_request_id(message: Message) -> None:
            nonlocal response_started, status_code
            if message["type"] == "http.response.start":
                response_started = True
                status_code = message["status"]
                headers = MutableHeaders(raw=message["headers"])
                headers[REQUEST_ID_HEADER] = request_id
            await send(message)

        structlog.contextvars.bind_contextvars(requestId=request_id)
        try:
            await self.app(scope, receive, send_with_request_id)
        except Exception:
            logger.exception(
                "request.completed",
                method=method,
                path=path,
                status=500,
                durationMs=_duration_ms(started),
                outcome="error",
            )
            if response_started:
                raise
            response = problem_response(
                status=500,
                code=ErrorCode.INTERNAL_ERROR,
                detail=GENERIC_INTERNAL_DETAIL,
                request_id=request_id,
            )
            await response(scope, receive, send_with_request_id)
        else:
            logger.info(
                "request.completed",
                method=method,
                path=path,
                status=status_code,
                durationMs=_duration_ms(started),
                outcome=_outcome(status_code),
            )
        finally:
            METRICS.increment(
                "http_requests_total",
                method=method,
                outcome=_outcome(status_code),
            )
            METRICS.observe(
                "http_request_duration_seconds",
                time.perf_counter() - started,
                method=method,
            )
            structlog.contextvars.clear_contextvars()
