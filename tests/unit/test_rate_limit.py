"""Fixed-window rate limiter behaviour."""

from __future__ import annotations

from app.core.rate_limit import _PRUNE_THRESHOLD, MemoryRateLimiter


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_allows_attempts_up_to_the_limit() -> None:
    limiter = MemoryRateLimiter(clock=FakeClock())

    results = [limiter.hit("ip", limit=3, window_seconds=60) for _ in range(3)]

    assert results == [None, None, None]


def test_blocks_the_attempt_after_the_limit() -> None:
    limiter = MemoryRateLimiter(clock=FakeClock())
    for _ in range(3):
        limiter.hit("ip", limit=3, window_seconds=60)

    retry_after = limiter.hit("ip", limit=3, window_seconds=60)

    assert retry_after == 60


def test_retry_after_counts_down_within_the_window() -> None:
    clock = FakeClock()
    limiter = MemoryRateLimiter(clock=clock)
    for _ in range(2):
        limiter.hit("ip", limit=2, window_seconds=120)

    clock.advance(30)

    assert limiter.hit("ip", limit=2, window_seconds=120) == 90


def test_window_resets_after_it_expires() -> None:
    clock = FakeClock()
    limiter = MemoryRateLimiter(clock=clock)
    for _ in range(2):
        limiter.hit("ip", limit=2, window_seconds=60)
    assert limiter.hit("ip", limit=2, window_seconds=60) == 60

    clock.advance(60)

    assert limiter.hit("ip", limit=2, window_seconds=60) is None


def test_keys_are_independent() -> None:
    limiter = MemoryRateLimiter(clock=FakeClock())
    for _ in range(2):
        limiter.hit("first", limit=2, window_seconds=60)

    assert limiter.hit("first", limit=2, window_seconds=60) == 60
    assert limiter.hit("second", limit=2, window_seconds=60) is None


def test_retry_after_is_never_zero() -> None:
    clock = FakeClock()
    limiter = MemoryRateLimiter(clock=clock)
    limiter.hit("ip", limit=1, window_seconds=60)

    clock.advance(59.9999)

    assert limiter.hit("ip", limit=1, window_seconds=60) == 1


def test_expired_windows_do_not_accumulate() -> None:
    clock = FakeClock()
    limiter = MemoryRateLimiter(clock=clock)
    for index in range(_PRUNE_THRESHOLD + 10):
        limiter.hit(f"key-{index}", limit=1, window_seconds=60)

    clock.advance(61)
    limiter.hit("fresh", limit=1, window_seconds=60)

    assert limiter.tracked_keys == 1
