"""Fixed-window rate limiting.

Two backends implement the same port:

- `PostgresRateLimiter` stores counters in `rate_limit_windows` with an atomic
  upsert. It is the default: an attempt budget survives a restart and is shared
  by every API process.
- `MemoryRateLimiter` keeps counters in the process. It is used by tests and is
  available for a throwaway local run.

A Redis backend remains the path for a high-write multi-instance deployment;
the port keeps callers independent of the store.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import case, delete, func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.db.models.rate_limit import RateLimitWindow
from app.db.session import session_scope

# Counters are cheaper to sweep in bulk than to expire lazily at the cost of
# unbounded memory. A sweep only happens above this many live keys.
_PRUNE_THRESHOLD = 4096

#: Expired database windows are kept this long before a startup prune removes
#: them, so a just-finished window is still visible to diagnostics.
PRUNE_AFTER_SECONDS = 3600


class RateLimiter(Protocol):
    """Record an attempt and report how long the caller must wait."""

    async def start(self) -> None:
        """Prepare the store. Called once from the application lifespan."""

    async def hit(self, key: str, *, limit: int, window_seconds: int) -> int | None:
        """Record one attempt.

        Returns `None` while the attempt is allowed. Once the limit is reached
        it returns the whole number of seconds the caller should wait, which is
        never zero so a retry cannot loop tightly.
        """


@dataclass
class _Window:
    started_at: float
    window_seconds: int
    count: int

    def expires_at(self) -> float:
        return self.started_at + self.window_seconds


class MemoryRateLimiter:
    """Per-key fixed windows of counters held in this process.

    A key is typically an IP address, or an IP plus an identifier such as an
    email. Callers decide the key so different endpoints can throttle
    independently.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._windows: dict[str, _Window] = {}

    async def start(self) -> None:
        """Nothing to prepare for an in-process store."""

    async def hit(self, key: str, *, limit: int, window_seconds: int) -> int | None:
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


class PostgresRateLimiter:
    """Fixed windows persisted in PostgreSQL and shared by every process.

    The upsert either starts a new window or increments the current one in one
    statement, so two concurrent requests cannot both observe the last slot.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock or _utcnow

    async def start(self) -> None:
        """Remove windows that expired more than a retention period ago."""
        cutoff = self._clock() - timedelta(seconds=PRUNE_AFTER_SECONDS)
        async with session_scope(self._session_factory) as session:
            await session.execute(
                delete(RateLimitWindow).where(RateLimitWindow.window_end < cutoff)
            )

    async def hit(self, key: str, *, limit: int, window_seconds: int) -> int | None:
        now = self._clock()
        window_end = now + timedelta(seconds=window_seconds)
        reset = RateLimitWindow.window_end <= now
        statement = (
            insert(RateLimitWindow)
            .values(key=key, window_end=window_end, count=1)
            .on_conflict_do_update(
                index_elements=[RateLimitWindow.key],
                set_={
                    "count": case(
                        (reset, 1),
                        else_=func.least(RateLimitWindow.count + 1, limit + 1),
                    ),
                    "window_end": case(
                        (reset, window_end),
                        else_=RateLimitWindow.window_end,
                    ),
                },
            )
            .returning(RateLimitWindow.count, RateLimitWindow.window_end)
        )
        async with session_scope(self._session_factory) as session:
            row = (await session.execute(statement)).one()
        count = int(row[0])
        end = row[1]
        if count <= limit:
            return None
        return max(1, math.ceil((end - now).total_seconds()))


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def build_rate_limiter(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> RateLimiter:
    """Build the limiter selected by `RATE_LIMIT_BACKEND`."""
    if settings.rate_limit_backend == "postgres":
        return PostgresRateLimiter(session_factory)
    return MemoryRateLimiter()
