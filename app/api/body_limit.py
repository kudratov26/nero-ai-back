"""Request body size cap.

Checks `Content-Length` first and then counts the bytes that actually arrive, so
a chunked request cannot slip past the limit.
"""

from __future__ import annotations

import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.problems import ErrorCode, problem_response

TOO_LARGE_DETAIL = "The request body is too large."


class _BodyTooLarge(Exception):
    """Raised inside the receive wrapper and caught by the middleware."""


def _declared_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers", []):
        if name == b"content-length":
            try:
                return int(bytes(value))
            except ValueError:
                return None
    return None


class RequestBodyLimitMiddleware:
    """Return `413 payload_too_large` before an oversized body is processed."""

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        state = scope.get("state")
        request_id = state.get("request_id") if isinstance(state, dict) else None
        response = problem_response(
            status=413,
            code=ErrorCode.PAYLOAD_TOO_LARGE,
            detail=TOO_LARGE_DETAIL,
            request_id=request_id
            if isinstance(request_id, str) and request_id
            else uuid.uuid4().hex,
        )
        await response(scope, receive, send)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = _declared_length(scope)
        if declared is not None and declared > self.max_bytes:
            await self._reject(scope, receive, send)
            return

        received = 0
        response_started = False

        async def send_tracked(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        async def receive_limited() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise _BodyTooLarge
            return message

        try:
            await self.app(scope, receive_limited, send_tracked)
        except _BodyTooLarge:
            if response_started:
                raise
            await self._reject(scope, receive, send)
