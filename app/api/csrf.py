"""Same-origin enforcement for state-changing requests.

Cookies are first-party through the Next rewrite, so an `Origin` header that
matches `FRONTEND_ORIGIN` (or the origin the request was addressed to) is enough
to stop cross-site form posts. A double-submit token is only needed for a
cross-site deployment, which would need an ADR first.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from starlette.types import ASGIApp, Receive, Scope, Send

from app.api.problems import ErrorCode, problem_response

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
MISSING_ORIGIN_DETAIL = "The Origin header is missing or not allowed."


def normalize_origin(value: str) -> str:
    """Reduce an origin to scheme, host, and port for comparison."""
    return value.strip().rstrip("/").lower()


def header_value(scope: Scope, name: str) -> str | None:
    """Return a request header by its lower-case name."""
    wanted = name.lower().encode("latin-1")
    for key, value in scope.get("headers", []):
        if key == wanted:
            return bytes(value).decode("latin-1")
    return None


def _first_forwarded(value: str | None) -> str | None:
    if not value:
        return None
    first = value.split(",", 1)[0].strip()
    return first or None


def request_origin(scope: Scope) -> str | None:
    """Best-effort origin of the URL the client addressed.

    Proxy headers win because TLS terminates upstream, so the ASGI scheme is
    usually `http` while the browser saw `https`.
    """
    host = _first_forwarded(header_value(scope, "x-forwarded-host")) or header_value(scope, "host")
    if not host:
        return None
    scheme = _first_forwarded(header_value(scope, "x-forwarded-proto")) or scope.get(
        "scheme", "http"
    )
    return f"{scheme}://{host}"


class OriginCheckMiddleware:
    """Reject unsafe methods whose `Origin` is missing or not allowlisted."""

    def __init__(self, app: ASGIApp, *, allowed_origins: Iterable[str]) -> None:
        self.app = app
        self.allowed = {normalize_origin(origin) for origin in allowed_origins if origin}

    def _allowed_for(self, scope: Scope) -> set[str]:
        allowed = set(self.allowed)
        own = request_origin(scope)
        if own:
            allowed.add(normalize_origin(own))
        return allowed

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in UNSAFE_METHODS:
            await self.app(scope, receive, send)
            return

        origin = header_value(scope, "origin")
        if origin is not None and normalize_origin(origin) in self._allowed_for(scope):
            await self.app(scope, receive, send)
            return

        state = scope.get("state")
        request_id = state.get("request_id") if isinstance(state, dict) else None
        response = problem_response(
            status=403,
            code=ErrorCode.ORIGIN_NOT_ALLOWED,
            detail=MISSING_ORIGIN_DETAIL,
            request_id=request_id
            if isinstance(request_id, str) and request_id
            else uuid.uuid4().hex,
        )
        response.headers["Vary"] = "Origin"
        await response(scope, receive, send)
