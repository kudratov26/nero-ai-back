"""Operational alerts and their delivery sinks.

`python -m app.cli ops-check` evaluates conditions and routes one `Alert` per
condition to every configured sink. Delivery failures are logged by type and
never stop the remaining alerts or sinks: an alerting path must not take the
checker down.

Alert details contain counts, durations, and fixed action hints only. They never
contain prompts, user content, tokens, or provider response bodies.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal, Protocol

import httpx
import structlog

log = structlog.get_logger(__name__)

SERVICE_NAME = "nero-ai-back"
WEBHOOK_TIMEOUT_SECONDS = 5.0

Severity = Literal["warning", "critical"]


class AlertCode(StrEnum):
    """Stable alert identifiers used by routing and runbooks."""

    READINESS_FAILED = "readiness_failed"
    QUEUE_BACKLOG = "queue_backlog"
    PROVIDER_ERROR_RATE = "provider_error_rate"
    CREDIT_INVARIANT_VIOLATION = "credit_invariant_violation"


@dataclass(frozen=True)
class Alert:
    """One condition that an operator should act on."""

    code: AlertCode
    severity: Severity
    summary: str
    detail: str | None = None
    detected_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))

    def as_payload(self) -> dict[str, str]:
        payload = {
            "service": SERVICE_NAME,
            "code": self.code.value,
            "severity": self.severity,
            "summary": self.summary,
            "detectedAt": self.detected_at.isoformat().replace("+00:00", "Z"),
        }
        if self.detail is not None:
            payload["detail"] = self.detail
        return payload


class AlertSink(Protocol):
    """A delivery channel for one alert."""

    async def send(self, alert: Alert) -> None:
        """Deliver one alert. Raises when delivery fails."""


class LogAlertSink:
    """Write an `ops.alert` structured event; the default sink."""

    async def send(self, alert: Alert) -> None:
        event = log.error if alert.severity == "critical" else log.warning
        event(
            "ops.alert",
            alert=alert.code.value,
            severity=alert.severity,
            summary=alert.summary,
            detail=alert.detail,
        )


class WebhookAlertSink:
    """POST the alert payload to a configured operator webhook.

    The URL is operator configuration and may carry a secret token, so it is
    never logged. Response bodies are discarded.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        timeout: float = WEBHOOK_TIMEOUT_SECONDS,
    ) -> None:
        self._client = client
        self._url = url
        self._timeout = timeout

    async def send(self, alert: Alert) -> None:
        response = await self._client.post(
            self._url,
            json=alert.as_payload(),
            timeout=self._timeout,
        )
        response.raise_for_status()


async def route_alerts(alerts: Sequence[Alert], sinks: Sequence[AlertSink]) -> int:
    """Send every alert to every sink; return the number of successful sends.

    One broken sink must not suppress the others, so a sink exception is logged
    and routing continues.
    """
    delivered = 0
    for alert in alerts:
        for sink in sinks:
            try:
                await sink.send(alert)
            except Exception as exc:
                log.error(
                    "ops.alert_sink_failed",
                    alert=alert.code.value,
                    sink=type(sink).__name__,
                    error=type(exc).__name__,
                )
                continue
            delivered += 1
    return delivered
