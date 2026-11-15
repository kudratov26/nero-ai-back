"""Credit ledger integration tests: reserve, commit, refund, grant, and reset.

These run against real PostgreSQL because the guarantees under test — row
locking, the check constraints, and concurrent serialization — only exist there.
"""

from __future__ import annotations

import asyncio
import random
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.api.problems import ApiError, ErrorCode
from app.db.models import CreditAccount, CreditLedgerEntry, User
from app.db.models.common import utcnow
from app.db.session import create_session_factory, session_scope
from app.domain import credits

ALLOWANCE = 50
PLAN_ID = "free"


@pytest.fixture
def factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(engine)


async def create_account(
    factory: async_sessionmaker[AsyncSession], *, allowance: int = ALLOWANCE
) -> UUID:
    """Create a user with a funded account and its opening grant."""
    async with session_scope(factory) as session:
        user = User(
            email=f"credits-{uuid4().hex}@example.com",
            display_name="Credits",
            password_hash="$argon2id$test-placeholder",
        )
        session.add(user)
        await session.flush()
        now = utcnow()
        session.add(
            CreditAccount(
                user_id=user.id,
                plan_id=PLAN_ID,
                balance=0,
                held=0,
                allowance=allowance,
                period_start=now,
                period_end=now + timedelta(days=30),
            )
        )
        await session.flush()
        await credits.grant(session, user_id=user.id, amount=allowance, reason="test allowance")
        return user.id


async def read_account(
    factory: async_sessionmaker[AsyncSession], user_id: UUID
) -> tuple[int, int, int]:
    """Read the committed account as `(balance, held, allowance)`."""
    async with session_scope(factory) as session:
        account = await session.get(CreditAccount, user_id)
        assert account is not None
        return account.balance, account.held, account.allowance


async def read_entries(
    factory: async_sessionmaker[AsyncSession], user_id: UUID
) -> list[CreditLedgerEntry]:
    """Read the account's ledger in write order."""
    async with session_scope(factory) as session:
        rows = await session.scalars(
            select(CreditLedgerEntry)
            .where(CreditLedgerEntry.user_id == user_id)
            .order_by(CreditLedgerEntry.created_at, CreditLedgerEntry.id)
        )
        return list(rows)


async def test_grant_adds_to_the_balance(factory: async_sessionmaker[AsyncSession]) -> None:
    user_id = await create_account(factory)

    async with session_scope(factory) as session:
        await credits.grant(session, user_id=user_id, amount=10, reason="bonus")

    assert await read_account(factory, user_id) == (60, 0, 50)
    last = (await read_entries(factory, user_id))[-1]
    assert (last.kind, last.delta_balance, last.delta_held) == ("grant", 10, 0)
    assert (last.balance_after, last.held_after) == (60, 0)


async def test_reserve_moves_credits_to_held(factory: async_sessionmaker[AsyncSession]) -> None:
    user_id = await create_account(factory)

    async with session_scope(factory) as session:
        account = await credits.reserve(session, user_id=user_id, amount=20, reason="run")
        assert credits.available(account) == 30

    assert await read_account(factory, user_id) == (50, 20, 50)
    last = (await read_entries(factory, user_id))[-1]
    assert (last.kind, last.delta_balance, last.delta_held) == ("reserve", 0, 20)
    assert (last.balance_after, last.held_after) == (50, 20)


async def test_reserve_of_the_exact_available_amount_is_allowed(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await create_account(factory)

    async with session_scope(factory) as session:
        await credits.reserve(session, user_id=user_id, amount=ALLOWANCE, reason="all in")

    balance, held, _ = await read_account(factory, user_id)
    assert balance - held == 0


async def test_reserve_beyond_available_is_rejected(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await create_account(factory)

    with pytest.raises(ApiError) as caught:
        async with session_scope(factory) as session:
            await credits.reserve(session, user_id=user_id, amount=51, reason="too much")

    assert caught.value.code == ErrorCode.INSUFFICIENT_CREDITS
    assert caught.value.status == 409
    assert caught.value.detail == "This run needs 51 Credits; 50 are available."
    # Nothing was written and the hold is intact.
    assert await read_account(factory, user_id) == (50, 0, 50)
    assert [entry.kind for entry in await read_entries(factory, user_id)] == ["grant"]


async def test_commit_consumes_a_hold(factory: async_sessionmaker[AsyncSession]) -> None:
    user_id = await create_account(factory)

    async with session_scope(factory) as session:
        await credits.reserve(session, user_id=user_id, amount=20, reason="run")
    async with session_scope(factory) as session:
        await credits.commit(session, user_id=user_id, amount=20, reason="done")

    assert await read_account(factory, user_id) == (30, 0, 50)
    last = (await read_entries(factory, user_id))[-1]
    assert (last.kind, last.delta_balance, last.delta_held) == ("commit", -20, -20)
    assert (last.balance_after, last.held_after) == (30, 0)


async def test_refund_returns_a_hold(factory: async_sessionmaker[AsyncSession]) -> None:
    user_id = await create_account(factory)

    async with session_scope(factory) as session:
        await credits.reserve(session, user_id=user_id, amount=20, reason="run")
    async with session_scope(factory) as session:
        await credits.refund(session, user_id=user_id, amount=20, reason="failed")

    assert await read_account(factory, user_id) == (50, 0, 50)
    last = (await read_entries(factory, user_id))[-1]
    assert (last.kind, last.delta_balance, last.delta_held) == ("refund", 0, -20)
    assert (last.balance_after, last.held_after) == (50, 0)


async def test_a_partial_commit_keeps_the_rest_held(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await create_account(factory)

    async with session_scope(factory) as session:
        await credits.reserve(session, user_id=user_id, amount=20, reason="run")
        await credits.commit(session, user_id=user_id, amount=8, reason="partial")
        # The remainder is refunded when the job settles smaller than reserved.
        await credits.refund(session, user_id=user_id, amount=12, reason="remainder")

    assert await read_account(factory, user_id) == (42, 0, 50)


@pytest.mark.parametrize("settle", ["commit", "refund"])
async def test_settling_more_than_held_is_a_programming_error(
    factory: async_sessionmaker[AsyncSession], settle: str
) -> None:
    user_id = await create_account(factory)
    async with session_scope(factory) as session:
        await credits.reserve(session, user_id=user_id, amount=5, reason="run")

    operation = credits.commit if settle == "commit" else credits.refund
    with pytest.raises(ValueError):
        async with session_scope(factory) as session:
            await operation(session, user_id=user_id, amount=6, reason="too much")

    assert await read_account(factory, user_id) == (50, 5, 50)


@pytest.mark.parametrize("amount", [0, -1])
async def test_non_positive_amounts_are_rejected(
    factory: async_sessionmaker[AsyncSession], amount: int
) -> None:
    user_id = await create_account(factory)

    for operation in (credits.grant, credits.reserve, credits.commit, credits.refund):
        with pytest.raises(ValueError):
            async with session_scope(factory) as session:
                await operation(session, user_id=user_id, amount=amount, reason="bad")

    assert await read_account(factory, user_id) == (50, 0, 50)


async def test_reset_refunds_holds_and_restores_the_allowance(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await create_account(factory)
    async with session_scope(factory) as session:
        await credits.reserve(session, user_id=user_id, amount=20, reason="hold")
        await credits.commit(session, user_id=user_id, amount=5, reason="spend")
    assert await read_account(factory, user_id) == (45, 15, 50)

    async with session_scope(factory) as session:
        await credits.reset(
            session, user_id=user_id, reason="demo reset", ref_type="demo_reset", ref_id=user_id
        )

    assert await read_account(factory, user_id) == (50, 0, 50)
    last = (await read_entries(factory, user_id))[-1]
    assert (last.kind, last.delta_balance, last.delta_held) == ("reset", 5, -15)
    assert (last.balance_after, last.held_after) == (50, 0)
    assert last.ref_type == "demo_reset"


async def test_reset_is_a_no_op_when_already_at_the_allowance(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await create_account(factory)
    before = len(await read_entries(factory, user_id))

    async with session_scope(factory) as session:
        await credits.reset(session, user_id=user_id, reason="noop")
        await credits.reset(session, user_id=user_id, reason="noop again")

    assert await read_account(factory, user_id) == (50, 0, 50)
    assert len(await read_entries(factory, user_id)) == before


async def test_a_ref_is_recorded_for_traceability(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await create_account(factory)
    ref = uuid4()

    async with session_scope(factory) as session:
        await credits.reserve(
            session,
            user_id=user_id,
            amount=3,
            reason="chat message",
            ref_type="chat_message",
            ref_id=ref,
        )

    last = (await read_entries(factory, user_id))[-1]
    assert last.ref_type == "chat_message"
    assert last.ref_id == ref


async def test_operations_require_an_account(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    with pytest.raises(RuntimeError):
        async with session_scope(factory) as session:
            await credits.grant(session, user_id=uuid4(), amount=1, reason="missing")


async def test_the_documented_lifecycle_walks_the_expected_states(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await create_account(factory)
    assert await read_account(factory, user_id) == (50, 0, 50)

    async with session_scope(factory) as session:
        await credits.reserve(session, user_id=user_id, amount=2, reason="reserve")
    assert await read_account(factory, user_id) == (50, 2, 50)

    async with session_scope(factory) as session:
        await credits.commit(session, user_id=user_id, amount=2, reason="commit")
    assert await read_account(factory, user_id) == (48, 0, 50)

    async with session_scope(factory) as session:
        await credits.reserve(session, user_id=user_id, amount=2, reason="reserve again")
        await credits.refund(session, user_id=user_id, amount=2, reason="refund instead")
    assert await read_account(factory, user_id) == (48, 0, 50)

    deltas = [
        (entry.kind, entry.delta_balance, entry.delta_held)
        for entry in await read_entries(factory, user_id)
    ]
    assert deltas == [
        ("grant", 50, 0),
        ("reserve", 0, 2),
        ("commit", -2, -2),
        ("reserve", 0, 2),
        ("refund", 0, -2),
    ]


async def test_two_concurrent_reserves_cannot_overspend(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await create_account(factory)
    outcomes: list[str] = []

    async def attempt() -> None:
        try:
            async with session_scope(factory) as session:
                await credits.reserve(session, user_id=user_id, amount=30, reason="concurrent")
            outcomes.append("reserved")
        except ApiError as exc:
            assert exc.code == ErrorCode.INSUFFICIENT_CREDITS
            outcomes.append("rejected")

    await asyncio.gather(attempt(), attempt())

    # The row lock serializes the two: one reserves, the other sees the hold.
    assert sorted(outcomes) == ["rejected", "reserved"]
    balance, held, _ = await read_account(factory, user_id)
    assert (balance, held) == (50, 30)
    assert balance - held == 20
    kinds = [entry.kind for entry in await read_entries(factory, user_id)]
    assert kinds.count("reserve") == 1


async def test_random_operations_preserve_the_invariants(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A long random walk never makes available negative or the ledger lie."""
    user_id = await create_account(factory)
    rng = random.Random(20261115)
    outstanding: list[int] = []

    for _ in range(80):
        choice = rng.choice(["grant", "reserve", "settle", "reset"])
        if choice == "grant":
            async with session_scope(factory) as session:
                await credits.grant(
                    session, user_id=user_id, amount=rng.randint(1, 20), reason="random"
                )
        elif choice == "reserve":
            amount = rng.randint(1, 20)
            try:
                async with session_scope(factory) as session:
                    await credits.reserve(session, user_id=user_id, amount=amount, reason="random")
            except ApiError as exc:
                assert exc.code == ErrorCode.INSUFFICIENT_CREDITS
                continue
            outstanding.append(amount)
        elif choice == "settle" and outstanding:
            amount = outstanding.pop(rng.randrange(len(outstanding)))
            settle = rng.choice([credits.commit, credits.refund])
            async with session_scope(factory) as session:
                await settle(session, user_id=user_id, amount=amount, reason="random")
        else:
            async with session_scope(factory) as session:
                await credits.reset(session, user_id=user_id, reason="random")
            outstanding.clear()

        balance, held, _ = await read_account(factory, user_id)
        assert balance >= 0
        assert held >= 0
        assert balance >= held

    balance, held, _ = await read_account(factory, user_id)
    entries = await read_entries(factory, user_id)
    # Replaying the append-only ledger reproduces the account exactly.
    assert sum(entry.delta_balance for entry in entries) == balance
    assert sum(entry.delta_held for entry in entries) == held
    assert entries[-1].balance_after == balance
    assert entries[-1].held_after == held
    assert all(entry.balance_after >= entry.held_after >= 0 for entry in entries)
