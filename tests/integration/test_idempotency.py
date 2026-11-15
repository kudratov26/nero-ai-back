"""Idempotency integration tests.

These run against real PostgreSQL: the `(user_id, key)` primary key and the
serialization that makes a concurrent duplicate safe only exist there.

Tests commit, so every assertion is scoped to a user or a per-test marker
rather than to a whole table.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.api.problems import ApiError, ErrorCode
from app.db.models import IdempotencyKey, User
from app.db.session import create_session_factory, session_scope
from app.domain import idempotency

BODY = {"modality": "image", "prompt": "A sunlit reading room"}
PASSWORD_HASH = "$argon2id$test-placeholder"


@pytest.fixture
def factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(engine)


async def add_user(factory: async_sessionmaker[AsyncSession], *, prefix: str = "idem") -> UUID:
    async with session_scope(factory) as session:
        user = User(
            email=f"{prefix}-{uuid4().hex}@example.com",
            display_name="Idempotency",
            password_hash=PASSWORD_HASH,
        )
        session.add(user)
        await session.flush()
        return user.id


def marker() -> str:
    """A unique label for the resources one test creates."""
    return f"created-{uuid4().hex}"


def creator(label: str) -> Callable[[AsyncSession], Awaitable[UUID]]:
    """A `create` callback that inserts a labelled User and returns its id."""

    async def create(session: AsyncSession) -> UUID:
        user = User(
            email=f"created-{uuid4().hex}@example.com",
            display_name=label,
            password_hash=PASSWORD_HASH,
        )
        session.add(user)
        await session.flush()
        return user.id

    return create


async def noop_create(session: AsyncSession) -> UUID:
    return uuid4()


async def count(factory: async_sessionmaker[AsyncSession], model: type, **filters: object) -> int:
    statement = select(func.count()).select_from(model)
    for column, value in filters.items():
        statement = statement.where(getattr(model, column) == value)
    async with session_scope(factory) as session:
        return await session.scalar(statement) or 0


def created(factory: async_sessionmaker[AsyncSession], label: str) -> Awaitable[int]:
    """Count the resources one test created, scoped by its label."""
    return count(factory, User, display_name=label)


def keys_for(factory: async_sessionmaker[AsyncSession], user_id: UUID) -> Awaitable[int]:
    return count(factory, IdempotencyKey, user_id=user_id)


async def run(
    factory: async_sessionmaker[AsyncSession],
    user_id: UUID,
    *,
    key: str = "key-1",
    operation: str = idempotency.GENERATION_CREATE,
    body: dict | None = None,
    create: Callable[[AsyncSession], Awaitable[UUID]] = noop_create,
) -> idempotency.CreateOutcome:
    return await idempotency.execute(
        factory,
        user_id=user_id,
        key=key,
        operation=operation,
        request_hash=idempotency.hash_request(BODY if body is None else body),
        create=create,
    )


async def test_a_first_call_creates_and_records(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await add_user(factory)
    label = marker()

    outcome = await run(factory, user_id, create=creator(label))

    assert outcome.replayed is False
    assert await created(factory, label) == 1
    async with session_scope(factory) as session:
        stored = await idempotency.find(session, user_id=user_id, key="key-1")
    assert stored is not None
    assert stored.operation == idempotency.GENERATION_CREATE
    assert stored.request_hash == idempotency.hash_request(BODY)
    assert stored.resource_id == outcome.resource_id
    assert stored.created_at.tzinfo is not None


async def test_a_replay_returns_the_original_without_creating(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await add_user(factory)
    label = marker()

    first = await run(factory, user_id, create=creator(label))
    second = await run(factory, user_id, create=creator(label))

    assert second.replayed is True
    assert second.resource_id == first.resource_id
    assert await created(factory, label) == 1
    assert await keys_for(factory, user_id) == 1


async def test_a_replay_does_not_call_create_again(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await add_user(factory)
    calls = 0

    async def counting_create(session: AsyncSession) -> UUID:
        nonlocal calls
        calls += 1
        return uuid4()

    await run(factory, user_id, create=counting_create)
    await run(factory, user_id, create=counting_create)

    assert calls == 1


async def test_a_different_body_is_a_conflict(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await add_user(factory)
    label = marker()
    await run(factory, user_id, create=creator(label))

    with pytest.raises(ApiError) as caught:
        await run(factory, user_id, body={"modality": "image", "prompt": "something else"})

    assert caught.value.code == ErrorCode.IDEMPOTENCY_CONFLICT
    assert caught.value.status == 409
    assert caught.value.detail == "This Idempotency-Key was already used with a different request."
    assert await created(factory, label) == 1


async def test_a_different_operation_is_a_conflict(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await add_user(factory)
    await run(factory, user_id, operation=idempotency.GENERATION_CREATE)

    with pytest.raises(ApiError) as caught:
        await run(factory, user_id, operation=idempotency.CHAT_MESSAGE)

    assert caught.value.code == ErrorCode.IDEMPOTENCY_CONFLICT


async def test_keys_are_scoped_to_the_user(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    first_user = await add_user(factory)
    second_user = await add_user(factory)

    first = await run(factory, first_user, key="shared")
    second = await run(factory, second_user, key="shared")

    assert first.resource_id != second.resource_id
    assert await keys_for(factory, first_user) == 1
    assert await keys_for(factory, second_user) == 1


async def test_an_unknown_key_is_none(factory: async_sessionmaker[AsyncSession]) -> None:
    user_id = await add_user(factory)

    async with session_scope(factory) as session:
        assert await idempotency.find(session, user_id=user_id, key="missing") is None


async def test_a_non_race_integrity_error_propagates_and_writes_nothing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await add_user(factory)
    taken = f"created-{uuid4().hex}@example.com"
    async with session_scope(factory) as session:
        session.add(User(email=taken, display_name="Taken", password_hash=PASSWORD_HASH))

    async def create(session: AsyncSession) -> UUID:
        session.add(User(email=taken, display_name="Clash", password_hash=PASSWORD_HASH))
        await session.flush()
        return uuid4()

    with pytest.raises(IntegrityError):
        await run(factory, user_id, create=create)

    # The failed request left no idempotency row behind.
    assert await keys_for(factory, user_id) == 0


async def test_concurrent_duplicates_create_one_resource(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await add_user(factory)
    label = marker()

    outcomes = await asyncio.gather(
        run(factory, user_id, create=creator(label)),
        run(factory, user_id, create=creator(label)),
    )

    assert len({outcome.resource_id for outcome in outcomes}) == 1
    assert sorted(outcome.replayed for outcome in outcomes) == [False, True]
    assert await created(factory, label) == 1
    assert await keys_for(factory, user_id) == 1


async def test_a_retry_that_also_loses_reraises(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id = await add_user(factory)
    label = marker()
    async with session_scope(factory) as session:
        session.add(
            IdempotencyKey(
                user_id=user_id,
                key="raced",
                operation=idempotency.GENERATION_CREATE,
                request_hash=idempotency.hash_request(BODY),
                resource_id=uuid4(),
            )
        )

    async def never_stored(*args: object, **kwargs: object) -> None:
        return None

    # Force the retry to lose again, exercising the exhaust path.
    monkeypatch.setattr(idempotency, "find", never_stored)

    with pytest.raises(IntegrityError):
        await run(factory, user_id, key="raced", create=creator(label))

    assert await created(factory, label) == 0
    assert await keys_for(factory, user_id) == 1
