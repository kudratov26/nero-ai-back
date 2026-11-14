"""Structured JSON logging with secret redaction.

Logs never contain request bodies, prompts, model output, session cookies, or
provider keys. This module configures structlog once at application startup and
redacts anything that still reaches a log event under a sensitive key.
"""

from __future__ import annotations

import logging
import sys
from typing import Any, cast

import structlog
from structlog.typing import EventDict, WrappedLogger

from app.core.config import Settings

REDACTED = "[REDACTED]"

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


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: REDACTED if _normalize(str(key)) in SENSITIVE_KEYS else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [_redact(item) for item in value]
    return value


def redact_secrets(_logger: WrappedLogger, _method_name: str, event_dict: EventDict) -> EventDict:
    """structlog processor that masks secrets at any nesting depth."""
    return cast(EventDict, _redact(dict(event_dict)))


def configure_logging(settings: Settings) -> None:
    """Configure structlog to emit one JSON object per line."""
    level = logging.getLevelNamesMapping()[settings.log_level.upper()]

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact_secrets,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
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
