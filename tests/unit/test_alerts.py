"""Alert payloads, routing, and sink isolation."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from app.core.alerts import (
    Alert,
    AlertCode,
    LogAlertSink,
    WebhookAlertSink,
    route_alerts,
)

DETECTED_AT = datetime(2026, 11, 21, 9, 30, tzinfo=UTC)


def make_alert(code: AlertCode = AlertCode.QUEUE_BACKLOG) -> Alert:
    return Alert(
        code=code,
        severity="warning",
        summary="3 Generation(s) queued.",
        detail="Check the worker.",
        detected_at=DETECTED_AT,
    )


class RecordingSink:
    def __init__(self) -> None:
        self.received: list[Alert] = []

    async def send(self, alert: Alert) -> None:
        self.received.append(alert)


class FailingSink:
    async def send(self, alert: Alert) -> None:
        raise RuntimeError("sink is down")


def test_alert_payload_is_camel_case_and_utc() -> None:
    payload = make_alert().as_payload()

    assert payload["service"] == "nero-ai-back"
    assert payload["code"] == "queue_backlog"
    assert payload["severity"] == "warning"
    assert payload["detectedAt"] == "2026-11-21T09:30:00Z"
    assert payload["detail"] == "Check the worker."


def test_alert_payload_omits_an_empty_detail() -> None:
    alert = Alert(
        code=AlertCode.READINESS_FAILED,
        severity="critical",
        summary="Not ready.",
        detected_at=DETECTED_AT,
    )

    assert "detail" not in alert.as_payload()


async def test_every_alert_reaches_every_sink() -> None:
    alerts = [make_alert(AlertCode.QUEUE_BACKLOG), make_alert(AlertCode.PROVIDER_ERROR_RATE)]
    first = RecordingSink()
    second = RecordingSink()

    delivered = await route_alerts(alerts, [first, second])

    assert delivered == 4
    assert first.received == alerts
    assert second.received == alerts


async def test_a_failing_sink_does_not_suppress_the_other(
    capsys: pytest.CaptureFixture[str],
) -> None:
    alert = make_alert()
    healthy = RecordingSink()

    delivered = await route_alerts([alert], [FailingSink(), healthy])

    assert delivered == 1
    assert healthy.received == [alert]
    events = [
        json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")
    ]
    assert any(event.get("event") == "ops.alert_sink_failed" for event in events)
    assert any(event.get("alert") == "queue_backlog" for event in events)


async def test_the_log_sink_writes_a_structured_alert(
    capsys: pytest.CaptureFixture[str],
) -> None:
    alert = Alert(
        code=AlertCode.CREDIT_INVARIANT_VIOLATION,
        severity="critical",
        summary="An account disagrees with the ledger.",
        detected_at=DETECTED_AT,
    )

    await LogAlertSink().send(alert)

    events = [
        json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")
    ]
    assert events[-1]["event"] == "ops.alert"
    assert events[-1]["level"] == "error"
    assert events[-1]["alert"] == "credit_invariant_violation"


async def test_the_webhook_posts_the_alert_payload() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sink = WebhookAlertSink(client, "https://hooks.example.test/alerts?token=secret")
        await sink.send(make_alert())

    assert len(seen) == 1
    assert seen[0].method == "POST"
    assert json.loads(seen[0].content)["code"] == "queue_backlog"


async def test_the_webhook_url_is_never_logged_on_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    url = "https://hooks.example.test/alerts?token=super-secret-token"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sink = WebhookAlertSink(client, url)
        delivered = await route_alerts([make_alert()], [sink, RecordingSink()])

    captured = capsys.readouterr().out
    assert delivered == 1
    assert "super-secret-token" not in captured
    assert "hooks.example.test" not in captured
