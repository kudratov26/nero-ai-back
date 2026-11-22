"""Operational alert checks against PostgreSQL.

Each test owns one account, so a leftover row from another suite cannot change
the counts. The checks are read-only; the tests only seed and clean up.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.alerts import AlertCode
from app.core.config import Settings
from app.db.models import Chat, CreditAccount, Generation, Message, User
from app.db.models.common import utcnow
from app.db.seed import IMAGE_MODEL_ID, upsert_models
from app.db.session import create_session_factory, session_scope
from app.domain import credits, ops
from tests.support import make_settings


@dataclass
class OpsEnv:
    factory: async_sessionmaker[AsyncSession]
    user_id: UUID
    settings: Settings


@pytest.fixture
async def ops_env(
    migrated_database: str,
    engine: AsyncEngine,
) -> AsyncIterator[OpsEnv]:
    settings = make_settings()
    factory = create_session_factory(engine)
    async with session_scope(factory) as session:
        # Other suites deliberately leave failed work and one account that
        # disagrees with its ledger. The checks are global by design, so start
        # from a state where only this fixture can trigger an alert.
        await session.execute(delete(Generation))
        await session.execute(delete(Message).where(Message.status == "failed"))
        await session.execute(
            text(
                """
                UPDATE credit_accounts AS a
                SET balance = COALESCE(
                        (SELECT SUM(e.delta_balance)
                         FROM credit_ledger_entries e
                         WHERE e.user_id = a.user_id), 0),
                    held = COALESCE(
                        (SELECT SUM(e.delta_held)
                         FROM credit_ledger_entries e
                         WHERE e.user_id = a.user_id), 0)
                """
            )
        )
        await upsert_models(session, settings)
        user = User(
            email=f"ops-{uuid4().hex}@example.com",
            display_name="Ops",
            password_hash="$argon2id$test-placeholder",
        )
        session.add(user)
        await session.flush()
        now = utcnow()
        session.add(
            CreditAccount(
                user_id=user.id,
                plan_id="free",
                balance=0,
                held=0,
                allowance=50,
                period_start=now,
                period_end=now + timedelta(days=30),
            )
        )
        user_id = user.id

    yield OpsEnv(factory=factory, user_id=user_id, settings=settings)

    async with session_scope(factory) as session:
        await session.execute(delete(User).where(User.id == user_id))


async def seed_generation(
    env: OpsEnv,
    *,
    status: str = "queued",
    age_seconds: float = 0,
    error_code: str | None = None,
) -> None:
    async with session_scope(env.factory) as session:
        now = utcnow()
        session.add(
            Generation(
                user_id=env.user_id,
                modality="image",
                model_id=IMAGE_MODEL_ID,
                prompt="A queued generation",
                params={"aspectRatio": "1:1", "count": 1, "style": "None"},
                status=status,
                progress=0,
                cost=0,
                provider="mock",
                error_code=error_code,
                created_at=now - timedelta(seconds=age_seconds),
                finished_at=now if status == "failed" else None,
            )
        )


async def seed_failed_message(env: OpsEnv, *, error_code: str = "provider_timeout") -> None:
    async with session_scope(env.factory) as session:
        chat = Chat(
            user_id=env.user_id,
            model_id=env.settings.ai_chat_model,
            title="Ops chat",
        )
        session.add(chat)
        await session.flush()
        session.add(
            Message(
                chat_id=chat.id,
                user_id=env.user_id,
                role="assistant",
                content="",
                model_id=env.settings.ai_chat_model,
                status="failed",
                error_code=error_code,
            )
        )


async def test_a_ready_database_does_not_alert(migrated_database: str, engine: AsyncEngine) -> None:
    assert await ops.check_readiness(engine) == []


async def test_an_unreachable_database_alerts(migrated_database: str) -> None:
    engine = create_async_engine("postgresql+asyncpg://nero:nero@127.0.0.1:1/nero_test")
    try:
        alerts = await ops.check_readiness(engine)
    finally:
        await engine.dispose()

    assert [alert.code for alert in alerts] == [AlertCode.READINESS_FAILED]
    assert alerts[0].severity == "critical"


async def test_too_many_aged_jobs_alert(ops_env: OpsEnv) -> None:
    settings = make_settings(
        ops_queue_backlog_threshold=2,
        ops_queue_backlog_age_seconds=60,
    )
    await seed_generation(ops_env, age_seconds=120)
    await seed_generation(ops_env, age_seconds=120)

    alerts = await ops.check_queue_backlog(ops_env.factory, settings)

    assert [alert.code for alert in alerts] == [AlertCode.QUEUE_BACKLOG]
    assert alerts[0].severity == "warning"
    assert "2 Generation(s)" in alerts[0].summary


async def test_young_or_absent_jobs_do_not_alert(ops_env: OpsEnv) -> None:
    settings = make_settings(
        ops_queue_backlog_threshold=2,
        ops_queue_backlog_age_seconds=60,
    )
    await seed_generation(ops_env, age_seconds=120)
    await seed_generation(ops_env, age_seconds=1)

    assert await ops.check_queue_backlog(ops_env.factory, settings) == []


async def test_provider_failures_within_the_window_alert(ops_env: OpsEnv) -> None:
    settings = make_settings(
        ops_provider_error_threshold=2,
        ops_provider_error_window_seconds=900,
    )
    await seed_generation(ops_env, status="failed", error_code="provider_error")
    await seed_failed_message(ops_env, error_code="provider_timeout")

    alerts = await ops.check_provider_errors(ops_env.factory, settings)

    assert [alert.code for alert in alerts] == [AlertCode.PROVIDER_ERROR_RATE]
    assert "2 provider failure(s)" in alerts[0].summary


async def test_client_errors_do_not_count_as_provider_failures(ops_env: OpsEnv) -> None:
    settings = make_settings(
        ops_provider_error_threshold=1,
        ops_provider_error_window_seconds=900,
    )
    await seed_generation(ops_env, status="failed", error_code="invalid_request")

    assert await ops.check_provider_errors(ops_env.factory, settings) == []


async def test_an_account_in_agreement_with_its_ledger_does_not_alert(
    ops_env: OpsEnv,
) -> None:
    async with session_scope(ops_env.factory) as session:
        await credits.grant(session, user_id=ops_env.user_id, amount=5, reason="test grant")

    assert await ops.check_credit_invariants(ops_env.factory) == []


async def test_an_account_disagreeing_with_its_ledger_alerts(ops_env: OpsEnv) -> None:
    async with session_scope(ops_env.factory) as session:
        account = await session.scalar(
            select(CreditAccount).where(CreditAccount.user_id == ops_env.user_id)
        )
        assert account is not None
        account.balance = 5

    alerts = await ops.check_credit_invariants(ops_env.factory)

    assert [alert.code for alert in alerts] == [AlertCode.CREDIT_INVARIANT_VIOLATION]
    assert alerts[0].severity == "critical"
    assert "1 Credit account(s)" in alerts[0].summary


async def test_collect_alerts_returns_every_condition(ops_env: OpsEnv, engine: AsyncEngine) -> None:
    settings = make_settings(
        ops_queue_backlog_threshold=1,
        ops_queue_backlog_age_seconds=60,
        ops_provider_error_threshold=1,
        ops_provider_error_window_seconds=900,
    )
    await seed_generation(ops_env, age_seconds=120)
    await seed_generation(ops_env, status="failed", error_code="provider_timeout")
    async with session_scope(ops_env.factory) as session:
        account = await session.scalar(
            select(CreditAccount).where(CreditAccount.user_id == ops_env.user_id)
        )
        assert account is not None
        account.balance = 5

    alerts = await ops.collect_alerts(engine, ops_env.factory, settings)

    assert {alert.code for alert in alerts} == {
        AlertCode.QUEUE_BACKLOG,
        AlertCode.PROVIDER_ERROR_RATE,
        AlertCode.CREDIT_INVARIANT_VIOLATION,
    }


async def test_collect_alerts_short_circuits_when_not_ready(
    migrated_database: str,
    engine: AsyncEngine,
) -> None:
    unreachable = create_async_engine("postgresql+asyncpg://nero:nero@127.0.0.1:1/nero_test")
    try:
        alerts = await ops.collect_alerts(
            unreachable,
            create_session_factory(engine),
            make_settings(),
        )
    finally:
        await unreachable.dispose()

    assert [alert.code for alert in alerts] == [AlertCode.READINESS_FAILED]
