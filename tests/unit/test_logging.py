"""Tests for structured logging and redaction."""

from __future__ import annotations

import json
from typing import Any

import pytest
import structlog

from app.core.config import Settings
from app.core.logging import (
    REDACTED,
    configure_logging,
    make_redactor,
    redact_secrets,
    secret_values,
)
from tests.support import make_settings

API_KEY = "sk-live-provider-key-123"
APP_SECRET = "app-secret-value-456"


@pytest.fixture
def settings() -> Settings:
    return make_settings()


def process(event_dict: dict[str, Any]) -> dict[str, Any]:
    return redact_secrets(None, "info", event_dict)  # type: ignore[arg-type]


def process_with_secrets(event_dict: dict[str, Any]) -> dict[str, Any]:
    settings = make_settings(ai_api_key=API_KEY, app_secret=APP_SECRET)
    redactor = make_redactor(secret_values(settings))
    return redactor(None, "info", event_dict)  # type: ignore[arg-type]


def test_redacts_sensitive_top_level_keys() -> None:
    event = process(
        {
            "event": "login",
            "authorization": "Bearer abc",
            "cookie": "nero_session=abc",
            "ai_api_key": "sk-live",
        }
    )
    assert event["event"] == "login"
    assert event["authorization"] == REDACTED
    assert event["cookie"] == REDACTED
    assert event["ai_api_key"] == REDACTED


def test_redacts_nested_headers() -> None:
    event = process({"headers": {"Authorization": "Bearer abc", "X-Request-ID": "r-1"}})
    assert event["headers"] == {"Authorization": REDACTED, "X-Request-ID": "r-1"}


def test_redacts_inside_lists() -> None:
    event = process({"pairs": [{"password": "hunter2"}, "safe"]})
    assert event["pairs"] == [{"password": REDACTED}, "safe"]


def test_leaves_ordinary_fields_untouched() -> None:
    event = process({"event": "health", "status": 200, "requestId": "r-1"})
    assert event == {"event": "health", "status": 200, "requestId": "r-1"}


def test_redacts_a_known_secret_inside_an_arbitrary_string() -> None:
    event = process_with_secrets(
        {
            "event": "provider.failed",
            "detail": f"upstream rejected {API_KEY}",
            "nested": {"message": f"app secret is {APP_SECRET} today"},
        }
    )

    assert event["detail"] == f"upstream rejected {REDACTED}"
    assert event["nested"]["message"] == f"app secret is {REDACTED} today"


def test_redacts_every_occurrence_of_a_secret() -> None:
    event = process_with_secrets({"event": "leak", "text": f"{API_KEY} and {API_KEY}"})

    assert event["text"] == f"{REDACTED} and {REDACTED}"


def test_a_short_secret_is_not_used_for_substring_redaction() -> None:
    settings = make_settings(app_secret="short", ai_api_key="also-short")
    redactor = make_redactor(secret_values(settings))

    event = redactor(None, "info", {"event": "kept", "detail": "a short message"})

    assert event["detail"] == "a short message"


def test_a_secret_under_a_sensitive_key_is_fully_masked() -> None:
    # The key rule must win over substring replacement, so a partial occurrence
    # never survives.
    event = process_with_secrets({"event": "request", "authorization": f"Bearer {API_KEY}"})

    assert event["authorization"] == REDACTED


def test_logs_are_json_lines(settings: Settings, capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(settings)
    structlog.get_logger("test").info(
        "provider.call", modelId="deepseek-v4.1-flash", api_key="sk-live", outcome="ok"
    )

    line = capsys.readouterr().out.strip()
    payload = json.loads(line)
    assert payload["event"] == "provider.call"
    assert payload["level"] == "info"
    assert payload["modelId"] == "deepseek-v4.1-flash"
    assert payload["api_key"] == REDACTED
    assert "timestamp" in payload


def test_a_secret_in_an_exception_is_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    """`format_exc_info` must run before redaction, or the traceback leaks."""
    configure_logging(make_settings(ai_api_key=API_KEY, app_secret=APP_SECRET))

    try:
        raise RuntimeError(f"upstream rejected {API_KEY}")
    except RuntimeError:
        structlog.get_logger("test").exception(
            "provider.failed",
            detail=f"key={API_KEY}",
        )

    line = capsys.readouterr().out.strip()
    payload = json.loads(line)
    assert API_KEY not in line
    assert APP_SECRET not in line
    assert payload["detail"] == f"key={REDACTED}"
    assert REDACTED in payload["exception"]
