"""PostgreSQL-backed rate limiting.

The limiter is exercised through its public `hit`/`start` methods against a real
database, because the guarantees under test — atomic counting under concurrency,
survival across instances, and a prune that does not grow forever — only exist
there. The final test proves the app wires the configured backend and returns a
429 when the budget is spent.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.rate_limit import (
    PRUNE_AFTER_SECONDS,
    MemoryRateLimiter,
    PostgresRateLimiter,
    build_rate_limiter,
)
from app.db.models import RateLimitWindow
from app.db.session import create_session_factory, session_scope
from app.main import create_app
from tests.support import asgi_client, assert_problem, make_settings, post_signup


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(engine)


@pytest.fixture
async def key(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[str]:
    value = f"rate-limit-{uuid4()}"
    try:
        yield value
    finally:
        async with session_scope(factory) as session:
            await session.execute(delete(RateLimitWindow).where(RateLimitWindow.key == value))


def limiter(
    factory: async_sessionmaker[AsyncSession], *, clock: FakeClock | None = None
) -> PostgresRateLimiter:
    return PostgresRateLimiter(
        factory,
        clock=clock or FakeClock(datetime(2026, 11, 20, tzinfo=UTC)),
    )


async def test_allows_up_to_the_limit_and_then_blocks(
    factory: async_sessionmaker[AsyncSession], key: str
) -> None:
    instance = limiter(factory)

    allowed = [await instance.hit(key, limit=3, window_seconds=60) for _ in range(3)]
    blocked = await instance.hit(key, limit=3, window_seconds=60)

    assert allowed == [None, None, None]
    assert blocked == 60


async def test_the_window_resets_after_it_expires(
    factory: async_sessionmaker[AsyncSession], key: str
) -> None:
    clock = FakeClock(datetime(2026, 11, 20, tzinfo=UTC))
    instance = limiter(factory, clock=clock)
    for _ in range(2):
        await instance.hit(key, limit=2, window_seconds=60)
    assert await instance.hit(key, limit=2, window_seconds=60) == 60

    clock.advance(60)

    assert await instance.hit(key, limit=2, window_seconds=60) is None


async def test_counters_survive_a_new_limiter_instance(
    factory: async_sessionmaker[AsyncSession], key: str
) -> None:
    """A restart must not clear an attacker's spent budget."""
    first = limiter(factory)
    for _ in range(2):
        assert await first.hit(key, limit=2, window_seconds=60) is None

    second = limiter(factory)

    assert await second.hit(key, limit=2, window_seconds=60) == 60


async def test_concurrent_hits_share_one_budget(
    factory: async_sessionmaker[AsyncSession], key: str
) -> None:
    instance = limiter(factory)

    results = await asyncio.gather(
        instance.hit(key, limit=1, window_seconds=60),
        instance.hit(key, limit=1, window_seconds=60),
    )

    assert sorted(result is None for result in results) == [False, True]


async def test_start_prunes_windows_older_than_the_retention(
    factory: async_sessionmaker[AsyncSession], key: str
) -> None:
    clock = FakeClock(datetime(2026, 11, 20, tzinfo=UTC))
    instance = limiter(factory, clock=clock)
    await instance.hit(key, limit=1, window_seconds=60)

    clock.advance(PRUNE_AFTER_SECONDS + 61)
    await instance.start()

    async with session_scope(factory) as session:
        remaining = await session.scalar(
            select(RateLimitWindow.key).where(RateLimitWindow.key == key)
        )
    assert remaining is None


def test_build_rate_limiter_honours_the_setting(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    postgres = build_rate_limiter(make_settings(rate_limit_backend="postgres"), factory)
    memory = build_rate_limiter(make_settings(rate_limit_backend="memory"), factory)

    assert isinstance(postgres, PostgresRateLimiter)
    assert isinstance(memory, MemoryRateLimiter)


async def test_the_app_uses_the_configured_backend_and_returns_429(
    engine: AsyncEngine,
) -> None:
    factory = create_session_factory(engine)
    # The signup key is per IP, so clear any budget a previous run left behind.
    async with session_scope(factory) as session:
        await session.execute(delete(RateLimitWindow))

    app = create_app(make_settings(rate_limit_backend="postgres"))
    async with asgi_client(app) as http:
        assert isinstance(app.state.rate_limiter, PostgresRateLimiter)
        for _ in range(3):
            response = await post_signup(http, f"limited-{uuid4().hex}@example.com")
            assert response.status_code == 201, response.text

        limited = await post_signup(http, f"limited-{uuid4().hex}@example.com")

    body = assert_problem(limited, 429, "rate_limited")
    assert body["detail"] == "Too many attempts. Try again later."
    assert limited.headers["retry-after"]
