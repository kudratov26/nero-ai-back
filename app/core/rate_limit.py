"""Fixed-window rate limiting.

State lives in the process, which is enough for a single instance. The backend is
selectable in settings so a Redis implementation can replace this one before the
API scales out, without changing callers.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass

# Counters are cheaper to sweep in bulk than to expire lazily at the cost of
# unbounded memory. A sweep only happens above this many live keys.
_PRUNE_THRESHOLD = 4096


@dataclass
class _Window:
    started_at: float
    window_seconds: int
    count: int

    def expires_at(self) -> float:
        return self.started_at + self.window_seconds


class MemoryRateLimiter:
    """Per-key fixed windows of counters.

    A key is typically an IP address, or an IP plus an identifier such as an
    email. Callers decide the key so different endpoints can throttle
    independently.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._windows: dict[str, _Window] = {}

    def hit(self, key: str, *, limit: int, window_seconds: int) -> int | None:
        """Record one attempt and report whether it is over the limit.

        Returns `None` while the attempt is allowed. Once the limit is reached it
        returns the whole number of seconds the caller should wait, which is
        never zero so a retry cannot loop tightly.
        """
        now = self._clock()
        window = self._windows.get(key)
        if window is None or now >= window.expires_at():
            self._maybe_prune(now)
            self._windows[key] = _Window(started_at=now, window_seconds=window_seconds, count=1)
            return None
        if window.count >= limit:
            return max(1, math.ceil(window.expires_at() - now))
        window.count += 1
        return None

    def _maybe_prune(self, now: float) -> None:
        if len(self._windows) < _PRUNE_THRESHOLD:
            return
        expired = [key for key, window in self._windows.items() if now >= window.expires_at()]
        for key in expired:
            del self._windows[key]

    @property
    def tracked_keys(self) -> int:
        """Number of live counters. Used by tests and diagnostics."""
        return len(self._windows)
