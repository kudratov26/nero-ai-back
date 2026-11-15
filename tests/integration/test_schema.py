"""Schema integration tests for users, sessions, plans, and Credit tables."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import generate_session_token, hash_session_token
from app.db.models import (
    CreditAccount,
    CreditLedgerEntry,
    IdempotencyKey,
    Model,
    Plan,
    User,
    UserSession,
)
from app.db.models.common import utcnow
from app.db.seed import PLAN_SEEDS, plan_values, upsert_plans

PLAN_FIELDS = (
    "id",
    "name",
    "price_cents",
    "monthly_credits",
    "tagline",
    "features",
    "highlighted",
    "sort_order",
)


def make_user(**overrides: Any) -> User:
    values: dict[str, Any] = {
        "email": "creator@example.com",
        "display_name": "Creator",
        "password_hash": "$argon2id$test-placeholder",
    }
    values.update(overrides)
    return User(**values)


def make_account(user_id: UUID, **overrides: Any) -> CreditAccount:
    now = utcnow()
    values: dict[str, Any] = {
        "user_id": user_id,
        "plan_id": "free",
        "balance": 50,
        "held": 0,
        "allowance": 50,
        "period_start": now,
        "period_end": now + timedelta(days=30),
    }
    values.update(overrides)
    return CreditAccount(**values)


def make_ledger_entry(user_id: UUID, **overrides: Any) -> CreditLedgerEntry:
    values: dict[str, Any] = {
        "user_id": user_id,
        "kind": "grant",
        "delta_balance": 50,
        "delta_held": 0,
        "balance_after": 50,
        "held_after": 0,
        "reason": "Free plan allowance",
    }
    values.update(overrides)
    return CreditLedgerEntry(**values)


def make_session(user_id: UUID, **overrides: Any) -> UserSession:
    values: dict[str, Any] = {
        "user_id": user_id,
        "token_hash": hash_session_token(generate_session_token()),
        "expires_at": utcnow() + timedelta(days=30),
    }
    values.update(overrides)
    return UserSession(**values)


def make_idempotency_key(user_id: UUID, **overrides: Any) -> IdempotencyKey:
    values: dict[str, Any] = {
        "user_id": user_id,
        "key": "key-1",
        "operation": "generation.create",
        "request_hash": "a" * 64,
        "resource_id": uuid4(),
    }
    values.update(overrides)
    return IdempotencyKey(**values)


def make_model(**overrides: Any) -> Model:
    values: dict[str, Any] = {
        "id": "test-model",
        "name": "Test Model",
        "provider": "Test",
        "provider_model_id": "test-model",
        "modality": "chat",
        "description": "A model used by the schema tests.",
        "badge": None,
        "credit_cost": 1,
        "credit_cost_10s": None,
        "capabilities": {"streaming": True},
        "context_window": 128_000,
        "available": True,
        "sort_order": 0,
    }
    values.update(overrides)
    return Model(**values)


async def add_user(session: AsyncSession, **overrides: Any) -> User:
    user = make_user(**overrides)
    session.add(user)
    await session.flush()
    return user


async def test_seeded_plans_match_seed_module(db_session: AsyncSession) -> None:
    rows = (await db_session.scalars(select(Plan).order_by(Plan.sort_order))).all()

    assert [{field: getattr(row, field) for field in PLAN_FIELDS} for row in rows] == plan_values()
    assert [plan.id for plan in PLAN_SEEDS] == ["free", "pro", "studio"]


async def test_upsert_plans_is_idempotent(db_session: AsyncSession) -> None:
    await upsert_plans(db_session)
    await upsert_plans(db_session)

    count = await db_session.scalar(select(func.count()).select_from(Plan))
    assert count == len(PLAN_SEEDS)


async def test_upsert_plans_restores_catalog_drift(db_session: AsyncSession) -> None:
    await db_session.execute(update(Plan).where(Plan.id == "free").values(price_cents=999))

    await upsert_plans(db_session)

    price = await db_session.scalar(select(Plan.price_cents).where(Plan.id == "free"))
    assert price == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("email", "Creator@Example.com"),
        ("display_name", ""),
        ("display_name", "x" * 81),
        ("role", "superuser"),
        ("status", "deleted"),
    ],
)
async def test_user_constraints(db_session: AsyncSession, field: str, value: str) -> None:
    db_session.add(make_user(**{field: value}))

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_user_email_is_unique(db_session: AsyncSession) -> None:
    db_session.add(make_user())
    await db_session.flush()

    db_session.add(make_user())

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_user_defaults_and_timestamps(db_session: AsyncSession) -> None:
    user = await add_user(db_session)
    await db_session.refresh(user)

    assert user.role == "user"
    assert user.status == "active"
    assert user.email_verified_at is None
    assert user.last_login_at is None
    assert user.created_at.tzinfo is not None
    assert user.created_at.utcoffset() == timedelta(0)
    assert user.updated_at.tzinfo is not None


async def test_session_accepts_valid_row(db_session: AsyncSession) -> None:
    user = await add_user(db_session)
    session = make_session(user.id, ip="203.0.113.7", user_agent="pytest")
    db_session.add(session)

    await db_session.flush()
    await db_session.refresh(session)

    assert session.ip == "203.0.113.7"
    assert session.created_at.tzinfo is not None
    assert session.revoked_at is None


async def test_session_token_hash_must_be_sha256_sized(db_session: AsyncSession) -> None:
    user = await add_user(db_session)
    db_session.add(make_session(user.id, token_hash=b"short"))

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_session_token_hash_is_unique(db_session: AsyncSession) -> None:
    user = await add_user(db_session)
    token_hash = hash_session_token(generate_session_token())
    db_session.add(make_session(user.id, token_hash=token_hash))
    await db_session.flush()

    db_session.add(make_session(user.id, token_hash=token_hash))

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_session_must_expire_after_creation(db_session: AsyncSession) -> None:
    user = await add_user(db_session)
    db_session.add(make_session(user.id, expires_at=utcnow() - timedelta(days=1)))

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_credit_account_accepts_valid_state(db_session: AsyncSession) -> None:
    user = await add_user(db_session)
    account = make_account(user.id)
    db_session.add(account)

    await db_session.flush()
    await db_session.refresh(account)

    assert (account.balance, account.held, account.allowance) == (50, 0, 50)
    assert account.updated_at.tzinfo is not None


async def test_credit_balance_cannot_be_negative(db_session: AsyncSession) -> None:
    user = await add_user(db_session)
    db_session.add(make_account(user.id, balance=-1))

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_credit_held_cannot_exceed_balance(db_session: AsyncSession) -> None:
    user = await add_user(db_session)
    db_session.add(make_account(user.id, balance=10, held=20))

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_credit_allowance_cannot_be_negative(db_session: AsyncSession) -> None:
    user = await add_user(db_session)
    db_session.add(make_account(user.id, allowance=-1))

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_credit_period_must_end_after_start(db_session: AsyncSession) -> None:
    user = await add_user(db_session)
    now = utcnow()
    db_session.add(make_account(user.id, period_start=now, period_end=now))

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_credit_account_requires_an_existing_plan(db_session: AsyncSession) -> None:
    user = await add_user(db_session)
    db_session.add(make_account(user.id, plan_id="nonexistent"))

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_plan_in_use_cannot_be_deleted(db_session: AsyncSession) -> None:
    user = await add_user(db_session)
    db_session.add(make_account(user.id))
    await db_session.flush()

    with pytest.raises(IntegrityError):
        await db_session.execute(delete(Plan).where(Plan.id == "free"))


async def test_ledger_accepts_valid_row(db_session: AsyncSession) -> None:
    user = await add_user(db_session)
    entry = make_ledger_entry(user.id, ref_type="plan", ref_id=user.id)
    db_session.add(entry)

    await db_session.flush()
    await db_session.refresh(entry)

    assert entry.created_at.tzinfo is not None
    assert entry.ref_type == "plan"


@pytest.mark.parametrize(
    "overrides",
    [
        {"kind": "mystery"},
        {"ref_type": "unknown"},
        {"balance_after": -1},
        {"held_after": -1},
        {"balance_after": 5, "held_after": 10},
    ],
)
async def test_ledger_constraints(db_session: AsyncSession, overrides: dict[str, Any]) -> None:
    user = await add_user(db_session)
    db_session.add(make_ledger_entry(user.id, **overrides))

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_deleting_a_user_cascades_owned_rows(db_session: AsyncSession) -> None:
    user = await add_user(db_session)
    user_id = user.id
    db_session.add_all(
        [
            make_account(user.id),
            make_ledger_entry(user.id),
            make_session(user.id),
        ]
    )
    await db_session.flush()

    await db_session.delete(user)
    await db_session.flush()

    for model in (CreditAccount, CreditLedgerEntry, UserSession):
        count = await db_session.scalar(
            select(func.count()).select_from(model).where(model.user_id == user_id)
        )
        assert count == 0, model.__tablename__


# --- Model catalog -----------------------------------------------------------


async def test_model_accepts_valid_row(db_session: AsyncSession) -> None:
    model = make_model(id="chat-model", modality="chat")
    db_session.add(model)

    await db_session.flush()
    await db_session.refresh(model)

    assert model.capabilities == {"streaming": True}
    assert model.available is True


async def test_model_defaults(db_session: AsyncSession) -> None:
    model = Model(
        id="defaults-model",
        name="Defaults",
        provider="Test",
        provider_model_id="defaults-model",
        modality="image",
        description="Uses column defaults.",
        credit_cost=5,
        sort_order=1,
    )
    db_session.add(model)

    await db_session.flush()
    await db_session.refresh(model)

    assert model.capabilities == {}
    assert model.available is False
    assert model.badge is None
    assert model.credit_cost_10s is None
    assert model.context_window is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"modality": "audio"},
        {"id": "   "},
        {"name": ""},
        {"credit_cost": -1},
        {"credit_cost_10s": -1, "modality": "video"},
        {"credit_cost_10s": 30, "modality": "image"},
        {"context_window": 0},
        {"context_window": -1},
        {"sort_order": -1},
    ],
)
async def test_model_constraints(db_session: AsyncSession, overrides: dict[str, Any]) -> None:
    db_session.add(make_model(**overrides))

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_model_id_is_unique(db_session: AsyncSession) -> None:
    db_session.add(make_model(id="duplicate-model"))
    await db_session.flush()

    db_session.add(make_model(id="duplicate-model", name="Other"))

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_video_model_accepts_a_10s_price(db_session: AsyncSession) -> None:
    db_session.add(make_model(id="video-model", modality="video", credit_cost_10s=30))

    await db_session.flush()


# --- Idempotency keys --------------------------------------------------------


async def test_idempotency_key_accepts_valid_row(db_session: AsyncSession) -> None:
    user = await add_user(db_session)
    key = make_idempotency_key(user.id, resource_id=user.id)
    db_session.add(key)

    await db_session.flush()
    await db_session.refresh(key)

    assert key.created_at.tzinfo is not None
    assert key.resource_id == user.id


async def test_idempotency_key_is_unique_per_user(db_session: AsyncSession) -> None:
    user = await add_user(db_session)
    db_session.add(make_idempotency_key(user.id))
    await db_session.flush()

    db_session.add(make_idempotency_key(user.id, resource_id=uuid4()))

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_the_same_key_may_be_used_by_another_user(db_session: AsyncSession) -> None:
    first = await add_user(db_session, email="first@example.com")
    second = await add_user(db_session, email="second@example.com")
    db_session.add_all(
        [make_idempotency_key(first.id), make_idempotency_key(second.id, resource_id=uuid4())]
    )

    await db_session.flush()

    count = await db_session.scalar(
        select(func.count())
        .select_from(IdempotencyKey)
        .where(IdempotencyKey.user_id.in_([first.id, second.id]))
    )
    assert count == 2


@pytest.mark.parametrize(
    "overrides",
    [
        {"key": "   "},
        {"key": "x" * 256},
        {"operation": ""},
        {"request_hash": "too-short"},
    ],
)
async def test_idempotency_key_constraints(
    db_session: AsyncSession, overrides: dict[str, Any]
) -> None:
    user = await add_user(db_session)
    db_session.add(make_idempotency_key(user.id, **overrides))

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_deleting_a_user_cascades_idempotency_keys(db_session: AsyncSession) -> None:
    user = await add_user(db_session)
    user_id = user.id
    db_session.add(make_idempotency_key(user_id))
    await db_session.flush()

    await db_session.delete(user)
    await db_session.flush()

    count = await db_session.scalar(
        select(func.count()).select_from(IdempotencyKey).where(IdempotencyKey.user_id == user_id)
    )
    assert count == 0
