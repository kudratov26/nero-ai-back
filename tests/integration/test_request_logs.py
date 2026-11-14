"""Access logs carry request context and never carry payloads or secrets."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import structlog

from app.core.logging import configure_logging
from tests.support import asgi_client, build_app, make_settings


def request_events(captured: str) -> list[dict[str, Any]]:
    return [
        payload
        for payload in (json.loads(line) for line in captured.splitlines() if line.startswith("{"))
        if payload.get("event") == "request.completed"
    ]


@pytest.fixture
async def client() -> httpx.AsyncClient:
    # Built inside the fixture so structlog writes to the captured stdout.
    async with asgi_client(build_app()) as http:
        yield http


async def test_access_log_has_request_context(
    client: httpx.AsyncClient, capsys: pytest.CaptureFixture[str]
) -> None:
    await client.get("/health/live", headers={"X-Request-ID": "req-log-1"})

    events = request_events(capsys.readouterr().out)
    assert len(events) == 1
    event = events[0]
    assert event["requestId"] == "req-log-1"
    assert event["method"] == "GET"
    assert event["path"] == "/health/live"
    assert event["status"] == 200
    assert event["outcome"] == "success"
    assert isinstance(event["durationMs"], int)


async def test_access_log_omits_payload_and_secrets(
    client: httpx.AsyncClient, capsys: pytest.CaptureFixture[str]
) -> None:
    await client.post(
        "/_test/echo",
        json={"email": "creator@example.com", "password": "super-secret-hunter2"},
        headers={
            "Authorization": "Bearer super-secret-token",
            "Cookie": "nero_session=super-secret-cookie",
        },
    )

    captured = capsys.readouterr().out
    events = request_events(captured)
    assert len(events) == 1
    assert events[0]["status"] == 200
    assert "super-secret-hunter2" not in captured
    assert "super-secret-token" not in captured
    assert "super-secret-cookie" not in captured


async def test_error_access_log_marks_outcome(
    client: httpx.AsyncClient, capsys: pytest.CaptureFixture[str]
) -> None:
    await client.get("/_test/boom", headers={"X-Request-ID": "req-log-500"})

    events = request_events(capsys.readouterr().out)
    assert len(events) == 1
    event = events[0]
    assert event["status"] == 500
    assert event["outcome"] == "error"
    assert event["requestId"] == "req-log-500"


async def test_client_error_access_log_marks_outcome(
    client: httpx.AsyncClient, capsys: pytest.CaptureFixture[str]
) -> None:
    await client.get("/v1/missing", headers={"X-Request-ID": "req-log-404"})

    events = request_events(capsys.readouterr().out)
    assert len(events) == 1
    assert events[0]["status"] == 404
    assert events[0]["outcome"] == "client_error"


def test_access_log_honors_log_level(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(make_settings(log_level="warning"))

    structlog.get_logger("app.request").info("request.completed", status=200)

    assert capsys.readouterr().out == ""
