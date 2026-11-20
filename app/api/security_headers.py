"""Baseline security headers for every HTTP response.

The API returns JSON and binary assets, so the headers are conservative: no
sniffing, no referrer leakage, and no framing. `Strict-Transport-Security` is
only sent in production, where TLS terminates at the reverse proxy; sending it
over local HTTP would make a browser refuse `http://localhost` for a year.

Routes may still replace these values deliberately; `setdefault` only fills a
header that the response did not set itself.
"""

from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

NOSNIFF = "nosniff"
NO_REFERRER = "no-referrer"
DENY = "DENY"
HSTS = "max-age=31536000; includeSubDomains"


class SecurityHeadersMiddleware:
    """Add the documented security headers to every response."""

    def __init__(self, app: ASGIApp, *, hsts: bool) -> None:
        self.app = app
        self.hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("X-Content-Type-Options", NOSNIFF)
                headers.setdefault("Referrer-Policy", NO_REFERRER)
                headers.setdefault("X-Frame-Options", DENY)
                if self.hsts:
                    headers.setdefault("Strict-Transport-Security", HSTS)
            await send(message)

        await self.app(scope, receive, send_with_headers)
