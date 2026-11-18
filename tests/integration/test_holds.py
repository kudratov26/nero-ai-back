"""Integration tests for the expired-hold sweeper.

The sweeper is the safety net for a reservation whose owner never settled it: a
chat handler killed mid-stream, or a Generation whose worker never came back. Each
test seeds the stranded row directly, runs the sweep, and then asserts the row, the
account, and the append-only ledger agree. The ledger is the source of truth for
"refunded exactly once", because an account balance alone cannot tell one refund
from two.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.db.models import (
    Asset,
    Chat,
    CreditAccount,
    CreditLedgerEntry,
    Generation,
    Message,
    User,
)
from app.db.models.common import utcnow
from app.db.seed import IMAGE_CREDITS, IMAGE_MODEL_ID, upsert_models
from app.db.session import create_session_factory, session_scope
from app.domain import credits, holds
from app.providers.fake_generation import FakeGenerationProvider
from app.workers.generation import run_worker
from tests.support import make_settings

HOLD_TTL = timedelta(seconds=3_600)
EXPIRED = timedelta(hours=2)
CHAT_MESSAGE_CREDITS = 1
STARTING_BALANCE = 100
FREE_PLAN = "free"


@dataclass
class HoldsEnv:
    factory: async_sessionmaker[AsyncSession]
    settings: Settings
    user_id: UUID
    chat_id: UUID


@pytest.fixture
async def holds_env(migrated_database: str, engine: AsyncEngine) -> AsyncIterator[HoldsEnv]:
    """One funded account and one chat, with an empty queue and message table."""
    settings = make_settings()
    factory = create_session_factory(engine)
    async with session_scope(factory) as session:
        await upsert_models(session, settings)
        user = User(
            email=f"holds-{uuid4().hex}@example.com",
            display_name="Holds",
            password_hash="$argon2id$test-placeholder",
        )
        session.add(user)
        await session.flush()
        now = utcnow()
        session.add(
            CreditAccount(
                user_id=user.id,
                plan_id=FREE_PLAN,
                balance=0,
                held=0,
                allowance=STARTING_BALANCE,
                period_start=now,
                period_end=now + timedelta(days=30),
            )
        )
        await session.flush()
        await credits.grant(
            session,
            user_id=user.id,
            amount=STARTING_BALANCE,
            reason="test allowance",
        )
        chat = Chat(user_id=user.id, title="Holds", model_id=settings.ai_chat_model)
        session.add(chat)
        await session.flush()
        env = HoldsEnv(
            factory=factory,
            settings=settings,
            user_id=user.id,
            chat_id=chat.id,
        )

    # The sweep is global by design; start from an empty queue so a leftover row
    # from another test cannot change the count or be swept by mistake.
    async with session_scope(factory) as session:
        await session.execute(delete(Asset))
        await session.execute(delete(Generation))
        await session.execute(delete(Message))

    yield env


async def seed_generation(
    env: HoldsEnv,
    *,
    status: str = "queued",
    created_at: datetime | None = None,
    locked_at: datetime | None = None,
    cost: int = IMAGE_CREDITS,
    settle: Literal["reserve", "commit", "refund", "none"] = "reserve",
) -> UUID:
    """Insert a Generation and settle its reservation the way the caller asks."""
    created = created_at or utcnow()
    async with session_scope(env.factory) as session:
        generation = Generation(
            user_id=env.user_id,
            modality="image",
            model_id=IMAGE_MODEL_ID,
            prompt="A sunlit reading room",
            params={"aspectRatio": "1:1", "count": 1, "style": "None"},
            status=status,
            progress=0,
            cost=cost,
            provider="Demo",
            created_at=created,
            locked_by="worker-gone" if status == "running" else None,
            locked_at=locked_at if status == "running" else None,
            started_at=created if status == "running" else None,
        )
        session.add(generation)
        await session.flush()
        if settle != "none" and cost > 0:
            await credits.reserve(
                session,
                user_id=env.user_id,
                amount=cost,
                reason="Image generation",
                ref_type="generation",
                ref_id=generation.id,
            )
        if settle == "commit":
            await credits.commit(
                session,
                user_id=env.user_id,
                amount=cost,
                reason="Image generation",
                ref_type="generation",
                ref_id=generation.id,
            )
        elif settle == "refund":
            await credits.refund(
                session,
                user_id=env.user_id,
                amount=cost,
                reason="Image generation",
                ref_type="generation",
                ref_id=generation.id,
            )
        return generation.id


async def seed_streaming_message(
    env: HoldsEnv,
    *,
    status: str = "streaming",
    updated_at: datetime | None = None,
    settle: Literal["reserve", "refund", "none"] = "reserve",
) -> UUID:
    """Insert an assistant turn with a reservation, as the message route does."""
    seen = updated_at or utcnow()
    async with session_scope(env.factory) as session:
        message = Message(
            chat_id=env.chat_id,
            user_id=env.user_id,
            role="assistant",
            content="",
            model_id=env.settings.ai_chat_model,
            status=status,
            created_at=seen,
            updated_at=seen,
        )
        session.add(message)
        await session.flush()
        if settle != "none":
            await credits.reserve(
                session,
                user_id=env.user_id,
                amount=CHAT_MESSAGE_CREDITS,
                reason="Chat message",
                ref_type="chat_message",
                ref_id=message.id,
            )
        if settle == "refund":
            await credits.refund(
                session,
                user_id=env.user_id,
                amount=CHAT_MESSAGE_CREDITS,
                reason="Chat message",
                ref_type="chat_message",
                ref_id=message.id,
            )
        return message.id


async def generation_row(env: HoldsEnv, generation_id: UUID) -> Generation:
    async with session_scope(env.factory) as session:
        row = await session.get(Generation, generation_id)
        assert row is not None
        return row


async def message_row(env: HoldsEnv, message_id: UUID) -> Message:
    async with session_scope(env.factory) as session:
        row = await session.get(Message, message_id)
        assert row is not None
        return row


async def account_row(env: HoldsEnv) -> CreditAccount:
    async with session_scope(env.factory) as session:
        row = await session.get(CreditAccount, env.user_id)
        assert row is not None
        return row


async def ledger_kinds(env: HoldsEnv, *, ref_type: str, ref_id: UUID) -> list[str]:
    async with session_scope(env.factory) as session:
        rows = await session.scalars(
            select(CreditLedgerEntry.kind)
            .where(
                CreditLedgerEntry.user_id == env.user_id,
                CreditLedgerEntry.ref_type == ref_type,
                CreditLedgerEntry.ref_id == ref_id,
            )
            .order_by(CreditLedgerEntry.created_at, CreditLedgerEntry.id)
        )
        return list(rows)


async def ledger_rows(env: HoldsEnv) -> list[CreditLedgerEntry]:
    async with session_scope(env.factory) as session:
        rows = await session.scalars(
            select(CreditLedgerEntry)
            .where(CreditLedgerEntry.user_id == env.user_id)
            .order_by(CreditLedgerEntry.created_at, CreditLedgerEntry.id)
        )
        return list(rows)


async def sweep(env: HoldsEnv) -> int:
    return await holds.sweep_expired_holds(env.factory, older_than=HOLD_TTL)


# --- Generations -------------------------------------------------------------


async def test_an_expired_queued_generation_is_failed_and_refunded(
    holds_env: HoldsEnv,
) -> None:
    before = utcnow()
    generation_id = await seed_generation(holds_env, created_at=utcnow() - EXPIRED)

    swept = await sweep(holds_env)

    assert swept == 1
    row = await generation_row(holds_env, generation_id)
    assert row.status == "failed"
    assert row.error_code == "hold_expired"
    assert row.finished_at is not None
    assert row.finished_at >= before
    assert await ledger_kinds(holds_env, ref_type="generation", ref_id=generation_id) == [
        "reserve",
        "refund",
    ]
    account = await account_row(holds_env)
    assert (account.balance, account.held) == (STARTING_BALANCE, 0)


async def test_an_expired_running_generation_loses_its_dead_lock(
    holds_env: HoldsEnv,
) -> None:
    stale = utcnow() - EXPIRED
    generation_id = await seed_generation(
        holds_env,
        status="running",
        created_at=stale,
        locked_at=stale,
    )

    swept = await sweep(holds_env)

    assert swept == 1
    row = await generation_row(holds_env, generation_id)
    assert row.status == "failed"
    assert row.error_code == "hold_expired"
    assert row.locked_by is None
    assert row.locked_at is None
    assert await ledger_kinds(holds_env, ref_type="generation", ref_id=generation_id) == [
        "reserve",
        "refund",
    ]
    account = await account_row(holds_env)
    assert (account.balance, account.held) == (STARTING_BALANCE, 0)


async def test_a_running_generation_with_a_fresh_lock_is_left_alone(
    holds_env: HoldsEnv,
) -> None:
    # The hold is old, but a worker claimed the job moments ago. Failing it would
    # throw away a render that is about to finish and refund work still underway.
    generation_id = await seed_generation(
        holds_env,
        status="running",
        created_at=utcnow() - EXPIRED,
        locked_at=utcnow(),
    )

    swept = await sweep(holds_env)

    assert swept == 0
    row = await generation_row(holds_env, generation_id)
    assert row.status == "running"
    assert row.locked_by == "worker-gone"
    assert await ledger_kinds(holds_env, ref_type="generation", ref_id=generation_id) == ["reserve"]
    account = await account_row(holds_env)
    assert (account.balance, account.held) == (STARTING_BALANCE, IMAGE_CREDITS)


async def test_the_sweep_is_idempotent(holds_env: HoldsEnv) -> None:
    generation_id = await seed_generation(holds_env, created_at=utcnow() - EXPIRED)

    assert await sweep(holds_env) == 1
    assert await sweep(holds_env) == 0

    assert await ledger_kinds(holds_env, ref_type="generation", ref_id=generation_id) == [
        "reserve",
        "refund",
    ]
    account = await account_row(holds_env)
    assert (account.balance, account.held) == (STARTING_BALANCE, 0)


async def test_a_settled_hold_fails_without_a_second_refund(holds_env: HoldsEnv) -> None:
    # The ledger is zero for the reference after a commit, so there is nothing to
    # refund even though the row is still queued.
    generation_id = await seed_generation(
        holds_env,
        created_at=utcnow() - EXPIRED,
        settle="commit",
    )

    swept = await sweep(holds_env)

    assert swept == 1
    row = await generation_row(holds_env, generation_id)
    assert row.status == "failed"
    assert row.error_code == "hold_expired"
    assert await ledger_kinds(holds_env, ref_type="generation", ref_id=generation_id) == [
        "reserve",
        "commit",
    ]
    account = await account_row(holds_env)
    assert (account.balance, account.held) == (STARTING_BALANCE - IMAGE_CREDITS, 0)


async def test_terminal_generations_are_never_swept(holds_env: HoldsEnv) -> None:
    cancelled = await seed_generation(
        holds_env,
        status="cancelled",
        created_at=utcnow() - EXPIRED,
        settle="refund",
    )
    succeeded = await seed_generation(
        holds_env,
        status="succeeded",
        created_at=utcnow() - EXPIRED,
        settle="commit",
    )

    assert await sweep(holds_env) == 0

    assert (await generation_row(holds_env, cancelled)).status == "cancelled"
    assert (await generation_row(holds_env, succeeded)).status == "succeeded"
    assert await ledger_kinds(holds_env, ref_type="generation", ref_id=cancelled) == [
        "reserve",
        "refund",
    ]
    assert await ledger_kinds(holds_env, ref_type="generation", ref_id=succeeded) == [
        "reserve",
        "commit",
    ]


async def test_a_hold_older_than_the_ttl_is_swept_without_going_negative(
    holds_env: HoldsEnv,
) -> None:
    first = await seed_generation(holds_env, created_at=utcnow() - EXPIRED)
    second = await seed_generation(holds_env, created_at=utcnow() - EXPIRED)

    assert await sweep(holds_env) == 2

    for generation_id in (first, second):
        assert await ledger_kinds(holds_env, ref_type="generation", ref_id=generation_id) == [
            "reserve",
            "refund",
        ]
    account = await account_row(holds_env)
    assert (account.balance, account.held) == (STARTING_BALANCE, 0)
    for entry in await ledger_rows(holds_env):
        assert entry.balance_after >= 0
        assert entry.held_after >= 0
        assert entry.balance_after >= entry.held_after


# --- Messages ----------------------------------------------------------------


async def test_an_expired_streaming_message_is_failed_and_refunded(
    holds_env: HoldsEnv,
) -> None:
    before = utcnow()
    message_id = await seed_streaming_message(holds_env, updated_at=utcnow() - EXPIRED)

    swept = await sweep(holds_env)

    assert swept == 1
    row = await message_row(holds_env, message_id)
    assert row.status == "failed"
    assert row.error_code == "hold_expired"
    assert row.updated_at >= before
    assert await ledger_kinds(holds_env, ref_type="chat_message", ref_id=message_id) == [
        "reserve",
        "refund",
    ]
    account = await account_row(holds_env)
    assert (account.balance, account.held) == (STARTING_BALANCE, 0)


async def test_a_fresh_streaming_message_is_left_alone(holds_env: HoldsEnv) -> None:
    message_id = await seed_streaming_message(holds_env)

    assert await sweep(holds_env) == 0

    row = await message_row(holds_env, message_id)
    assert row.status == "streaming"
    assert await ledger_kinds(holds_env, ref_type="chat_message", ref_id=message_id) == ["reserve"]
    account = await account_row(holds_env)
    assert (account.balance, account.held) == (
        STARTING_BALANCE,
        CHAT_MESSAGE_CREDITS,
    )


async def test_a_completed_message_is_not_swept(holds_env: HoldsEnv) -> None:
    message_id = await seed_streaming_message(
        holds_env,
        status="complete",
        updated_at=utcnow() - EXPIRED,
    )

    assert await sweep(holds_env) == 0

    row = await message_row(holds_env, message_id)
    assert row.status == "complete"
    assert await ledger_kinds(holds_env, ref_type="chat_message", ref_id=message_id) == ["reserve"]


async def test_a_message_with_a_settled_hold_is_failed_without_a_second_refund(
    holds_env: HoldsEnv,
) -> None:
    message_id = await seed_streaming_message(
        holds_env,
        updated_at=utcnow() - EXPIRED,
        settle="refund",
    )

    assert await sweep(holds_env) == 1

    row = await message_row(holds_env, message_id)
    assert row.status == "failed"
    assert row.error_code == "hold_expired"
    assert await ledger_kinds(holds_env, ref_type="chat_message", ref_id=message_id) == [
        "reserve",
        "refund",
    ]
    account = await account_row(holds_env)
    assert (account.balance, account.held) == (STARTING_BALANCE, 0)


async def test_one_sweep_settles_generations_and_messages(holds_env: HoldsEnv) -> None:
    generation_id = await seed_generation(holds_env, created_at=utcnow() - EXPIRED)
    message_id = await seed_streaming_message(holds_env, updated_at=utcnow() - EXPIRED)

    assert await sweep(holds_env) == 2

    assert (await generation_row(holds_env, generation_id)).status == "failed"
    assert (await message_row(holds_env, message_id)).status == "failed"
    account = await account_row(holds_env)
    assert (account.balance, account.held) == (STARTING_BALANCE, 0)


# --- Worker wiring -----------------------------------------------------------


async def test_the_worker_sweeps_expired_holds_before_claiming(
    holds_env: HoldsEnv,
) -> None:
    # `run_worker` builds its own engine from settings, so point it at the same
    # database and storage the fixture uses.
    generation_id = await seed_generation(holds_env, created_at=utcnow() - EXPIRED)
    settings = make_settings(
        worker_concurrency=1,
        worker_poll_interval_seconds=0.01,
        hold_ttl_seconds=HOLD_TTL.total_seconds(),
    )
    provider = FakeGenerationProvider()
    stop = asyncio.Event()

    async def on_idle() -> None:
        stop.set()

    await run_worker(settings, provider, stop=stop, on_idle=on_idle)

    # The expired hold is refunded, not rendered.
    assert provider.requests == []
    row = await generation_row(holds_env, generation_id)
    assert row.status == "failed"
    assert row.error_code == "hold_expired"
    account = await account_row(holds_env)
    assert (account.balance, account.held) == (STARTING_BALANCE, 0)
