"""Structured JSON logging with secret redaction.

Logs never contain request bodies, prompts, model output, session cookies, or
provider keys. Redaction works at two levels:

- Any value under a sensitive key is masked, at any nesting depth.
- Known secret values (the provider key and the app secret) are replaced
  wherever they appear inside a string.

`format_exc_info` runs before the redactor, so an exception message or traceback
that quotes a key is masked as well.
"""

from __future__ import annotations

import logging
import sys
from typing import Any, cast

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

from app.core.config import Settings

REDACTED = "[REDACTED]"

#: Values shorter than this are not used for substring redaction. A trivial
#: secret would otherwise mangle unrelated log messages.
MIN_REDACTABLE_SECRET_LENGTH = 8

# Keys are compared after `_normalize` (lowercase, dashes to underscores).
SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "authorization",
        "cookie",
        "set_cookie",
        "api_key",
        "ai_api_key",
        "app_secret",
        "password",
        "current_password",
        "new_password",
        "token",
        "token_hash",
        "access_token",
        "refresh_token",
        "session_token",
        "provider_key",
        "secret",
    }
)


def _normalize(key: str) -> str:
    return key.lower().replace("-", "_")


def _redact(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    if isinstance(value, dict):
        return {
            key: REDACTED if _normalize(str(key)) in SENSITIVE_KEYS else _redact(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, str) and secrets:
        for secret in secrets:
            value = value.replace(secret, REDACTED)
        return value
    return value


def redact_secrets(_logger: WrappedLogger, _method_name: str, event_dict: EventDict) -> EventDict:
    """structlog processor that masks sensitive keys at any nesting depth."""
    return cast(EventDict, _redact(dict(event_dict)))


def secret_values(settings: Settings) -> tuple[str, ...]:
    """Configured values that must never appear in a log line, longest first."""
    values: set[str] = set()
    for candidate in (settings.app_secret, settings.ai_api_key):
        if candidate and len(candidate) >= MIN_REDACTABLE_SECRET_LENGTH:
            values.add(candidate)
    # Longest first so a secret that contains another is replaced whole.
    return tuple(sorted(values, key=len, reverse=True))


def make_redactor(secrets: tuple[str, ...]) -> Processor:
    """Build a processor that masks sensitive keys and known values."""

    def redactor(_logger: WrappedLogger, _method_name: str, event_dict: EventDict) -> EventDict:
        return cast(EventDict, _redact(dict(event_dict), secrets))

    return redactor


def configure_logging(settings: Settings) -> None:
    """Configure structlog to emit one JSON object per line."""
    level = logging.getLevelNamesMapping()[settings.log_level.upper()]

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            # Format first: the traceback is then a plain string the redactor can
            # scan for a key quoted in an exception message.
            structlog.processors.format_exc_info,
            make_redactor(secret_values(settings)),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )

    logging.basicConfig(
        level=level,
        stream=sys.stdout,
        format="%(message)s",
        force=True,
    )
