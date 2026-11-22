"""In-process metrics with Prometheus text exposition.

The API and worker are single-instance processes, so metrics live in memory and a
collector scrapes each process separately. Nothing here depends on a metrics
service; the scrape endpoint is the only egress.

Names and labels are fixed in code, never taken from a request, so cardinality
stays bounded. Values are counts and durations only: no prompt, user id, token,
or provider body can reach a metric.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Buckets for request and provider durations, in seconds. They cover a health
#: check through a slow streaming call; `+Inf` is added at render time.
DEFAULT_BUCKETS: tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
)

_NAME_PATTERN = re.compile(r"^[A-Za-z_:][A-Za-z0-9_:]*$")
_LABEL_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

LabelSet = tuple[tuple[str, str], ...]
CounterKey = tuple[str, LabelSet]
HistogramKey = tuple[str, LabelSet]


def _labels(**labels: str) -> LabelSet:
    for key in labels:
        if not _LABEL_PATTERN.fullmatch(key):
            raise ValueError(f"invalid metric label name: {key!r}")
    return tuple(sorted(labels.items()))


def _render_labels(labels: LabelSet, extra: tuple[str, str] | None = None) -> str:
    pairs = list(labels)
    if extra is not None:
        pairs.append(extra)
    if not pairs:
        return ""
    rendered = ",".join(f'{key}="{_escape(value)}"' for key, value in sorted(pairs))
    return "{" + rendered + "}"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _format_bound(value: float) -> str:
    return f"{value:g}"


@dataclass
class _Histogram:
    """Cumulative bucket counts plus the total sum and observation count."""

    buckets: list[int] = field(default_factory=lambda: [0] * len(DEFAULT_BUCKETS))
    count: int = 0
    total: float = 0.0

    def observe(self, value: float) -> None:
        self.count += 1
        self.total += value
        for index, bound in enumerate(DEFAULT_BUCKETS):
            if value <= bound:
                self.buckets[index] += 1


class Metrics:
    """A minimal counter/histogram registry.

    `increment` records a counter sample; `observe` records a duration in seconds.
    Metric and label names are validated, so a typo fails in a test instead of
    producing an unparseable scrape.
    """

    def __init__(self) -> None:
        self._counters: dict[CounterKey, int] = {}
        self._histograms: dict[HistogramKey, _Histogram] = {}

    def increment(self, name: str, value: int = 1, **labels: str) -> None:
        self._check_name(name)
        key = (name, _labels(**labels))
        self._counters[key] = self._counters.get(key, 0) + value

    def observe(self, name: str, value: float, **labels: str) -> None:
        self._check_name(name)
        key = (name, _labels(**labels))
        histogram = self._histograms.get(key)
        if histogram is None:
            histogram = _Histogram()
            self._histograms[key] = histogram
        histogram.observe(value)

    def reset(self) -> None:
        """Clear every series. Used by tests and never by the running app."""
        self._counters.clear()
        self._histograms.clear()

    def render(self) -> str:
        """Render the Prometheus text exposition format."""
        lines: list[str] = []
        for name in sorted({name for name, _ in self._counters}):
            lines.append(f"# TYPE {name} counter")
            for (series, labels), value in sorted(self._counters.items()):
                if series == name:
                    lines.append(f"{name}{_render_labels(labels)} {value}")

        for name in sorted({name for name, _ in self._histograms}):
            lines.append(f"# TYPE {name} histogram")
            for (series, labels), histogram in sorted(self._histograms.items()):
                if series != name:
                    continue
                for bound, count in zip(DEFAULT_BUCKETS, histogram.buckets, strict=True):
                    rendered = _render_labels(labels, ("le", _format_bound(bound)))
                    lines.append(f"{name}_bucket{rendered} {count}")
                rendered = _render_labels(labels, ("le", "+Inf"))
                lines.append(f"{name}_bucket{rendered} {histogram.count}")
                rendered = _render_labels(labels)
                lines.append(f"{name}_sum{rendered} {histogram.total:.6f}")
                lines.append(f"{name}_count{rendered} {histogram.count}")

        return "" if not lines else "\n".join(lines) + "\n"

    @staticmethod
    def _check_name(name: str) -> None:
        if not _NAME_PATTERN.fullmatch(name):
            raise ValueError(f"invalid metric name: {name!r}")


#: Process-wide registry. The API and worker are separate processes, so each has
#: its own instance; tests reset it to keep assertions deterministic.
METRICS = Metrics()
