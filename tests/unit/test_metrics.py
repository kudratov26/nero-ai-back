"""Metrics registry and Prometheus text exposition."""

from __future__ import annotations

import pytest

from app.core.metrics import DEFAULT_BUCKETS, Metrics


def test_an_empty_registry_renders_nothing() -> None:
    assert Metrics().render() == ""


def test_a_counter_accumulates_per_label_set() -> None:
    metrics = Metrics()
    metrics.increment("requests_total", method="GET", outcome="success")
    metrics.increment("requests_total", method="GET", outcome="success")
    metrics.increment("requests_total", method="POST", outcome="error")

    rendered = metrics.render()

    assert "# TYPE requests_total counter" in rendered
    assert 'requests_total{method="GET",outcome="success"} 2' in rendered
    assert 'requests_total{method="POST",outcome="error"} 1' in rendered


def test_a_counter_can_add_more_than_one() -> None:
    metrics = Metrics()
    metrics.increment("bytes_total", value=7)
    metrics.increment("bytes_total", value=5)

    assert "bytes_total 12" in metrics.render()


def test_a_histogram_renders_cumulative_buckets_sum_and_count() -> None:
    metrics = Metrics()
    metrics.observe("duration_seconds", 0.02)
    metrics.observe("duration_seconds", 5.0)

    rendered = metrics.render()

    assert "# TYPE duration_seconds histogram" in rendered
    assert 'duration_seconds_bucket{le="0.005"} 0' in rendered
    assert 'duration_seconds_bucket{le="0.025"} 1' in rendered
    assert 'duration_seconds_bucket{le="5"} 2' in rendered
    assert 'duration_seconds_bucket{le="+Inf"} 2' in rendered
    assert "duration_seconds_sum 5.020000" in rendered
    assert "duration_seconds_count 2" in rendered


def test_every_configured_bucket_is_rendered() -> None:
    metrics = Metrics()
    metrics.observe("duration_seconds", 0.001)

    rendered = metrics.render()

    buckets = [line for line in rendered.splitlines() if "_bucket{" in line]
    assert len(buckets) == len(DEFAULT_BUCKETS) + 1
    assert 'duration_seconds_bucket{le="+Inf"} 1' in rendered


def test_label_values_are_escaped() -> None:
    metrics = Metrics()
    metrics.increment("escaped_total", label='a"b\nc\\d')

    assert 'escaped_total{label="a\\"b\\nc\\\\d"} 1' in metrics.render()


def test_invalid_metric_names_fail_fast() -> None:
    with pytest.raises(ValueError, match="invalid metric name"):
        Metrics().increment("not a name")

    with pytest.raises(ValueError, match="invalid metric name"):
        Metrics().observe("1leading-digit", 0.1)


def test_invalid_label_names_fail_fast() -> None:
    with pytest.raises(ValueError, match="invalid metric label name"):
        Metrics().increment("ok_total", **{"bad-label": "x"})


def test_reset_clears_every_series() -> None:
    metrics = Metrics()
    metrics.increment("requests_total")
    metrics.observe("duration_seconds", 0.1)

    metrics.reset()

    assert metrics.render() == ""
