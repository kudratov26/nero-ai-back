"""Tests for structured logging and redaction."""

from __future__ import annotations

import json
from typing import Any

import pytest
import structlog

from app.core.config import Settings
from app.core.logging import REDACTED, configure_logging, redact_secrets
from tests.support import make_settings


@pytest.fixture
def settings() -> Settings:
    return make_settings()


def process(event_dict: dict[str, Any]) -> dict[str, Any]:
    return redact_secrets(None, "info", event_dict)  # type: ignore[arg-type]


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
