"""The public scrape endpoint and the HTTP samples it exposes."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from app.core.metrics import METRICS
from tests.support import asgi_client, build_app


@pytest.fixture(autouse=True)
def clean_metrics() -> Iterator[None]:
    """The registry is process-wide; keep each test's assertions exact."""
    METRICS.reset()
    yield
    METRICS.reset()


async def test_metrics_returns_prometheus_text_with_http_samples() -> None:
    async with asgi_client(build_app()) as client:
        assert (await client.get("/health/live")).status_code == 200
        response = await client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert 'http_requests_total{method="GET",outcome="success"} 1' in response.text
    assert "http_request_duration_seconds_bucket" in response.text
    assert "http_request_duration_seconds_count" in response.text


async def test_metrics_is_public_and_carries_no_user_data() -> None:
    async with asgi_client(build_app()) as client:
        response = await client.get("/metrics")

    assert response.status_code == 200
    assert "userId" not in response.text
    assert "email" not in response.text.lower()
    assert "@example.com" not in response.text
