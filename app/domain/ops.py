"""Operational checks behind `python -m app.cli ops-check`.

Four conditions from the roadmap are evaluated against the database:

- readiness: connectivity plus migrations at head;
- queue backlog: queued Generations older than the configured age;
- provider errors: failed chat messages and Generations in a recent window;
- Credit invariant: an account balance/held pair that disagrees with its ledger.

The checks read only; they never settle work, move Credits, or call a provider.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.api.problems import ErrorCode
from app.core.alerts import Alert, AlertCode
from app.core.config import Settings
from app.core.readiness import database_is_ready
from app.db.models import CreditAccount, CreditLedgerEntry, Generation, Message
from app.db.models.common import utcnow
from app.db.session import session_scope

#: Failure codes that mean the provider, not the request, was at fault.
PROVIDER_ERROR_CODES = frozenset(
    {
        ErrorCode.PROVIDER_ERROR.value,
        ErrorCode.PROVIDER_TIMEOUT.value,
        ErrorCode.RATE_LIMITED.value,
    }
)


async def check_readiness(engine: AsyncEngine) -> list[Alert]:
    """Alert when the database is unreachable or migrations are not at head."""
    if await database_is_ready(engine):
        return []
    return [
        Alert(
            code=AlertCode.READINESS_FAILED,
            severity="critical",
            summary="The service is not ready.",
            detail="The database is unreachable or migrations are pending.",
        )
    ]


async def check_queue_backlog(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> list[Alert]:
    """Alert when too many Generations have waited longer than the threshold."""
    cutoff = utcnow() - timedelta(seconds=settings.ops_queue_backlog_age_seconds)
    async with session_scope(session_factory) as session:
        count = await session.scalar(
            select(func.count())
            .select_from(Generation)
            .where(
                Generation.status == "queued",
                Generation.created_at < cutoff,
            )
        )
    waiting = int(count or 0)
    if waiting < settings.ops_queue_backlog_threshold:
        return []
    age = int(settings.ops_queue_backlog_age_seconds)
    return [
        Alert(
            code=AlertCode.QUEUE_BACKLOG,
            severity="warning",
            summary=f"{waiting} Generation(s) queued for more than {age}s.",
            detail="Check that the worker process is running and the provider is healthy.",
        )
    ]


async def check_provider_errors(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> list[Alert]:
    """Alert when provider failures exceed the threshold inside the window."""
    cutoff = utcnow() - timedelta(seconds=settings.ops_provider_error_window_seconds)
    async with session_scope(session_factory) as session:
        failed_generations = await session.scalar(
            select(func.count())
            .select_from(Generation)
            .where(
                Generation.status == "failed",
                Generation.error_code.in_(PROVIDER_ERROR_CODES),
                Generation.finished_at >= cutoff,
            )
        )
        failed_messages = await session.scalar(
            select(func.count())
            .select_from(Message)
            .where(
                Message.status == "failed",
                Message.error_code.in_(PROVIDER_ERROR_CODES),
                Message.updated_at >= cutoff,
            )
        )
    failures = int(failed_generations or 0) + int(failed_messages or 0)
    if failures < settings.ops_provider_error_threshold:
        return []
    minutes = int(settings.ops_provider_error_window_seconds / 60)
    return [
        Alert(
            code=AlertCode.PROVIDER_ERROR_RATE,
            severity="warning",
            summary=f"{failures} provider failure(s) in the last {minutes} minute(s).",
            detail=(
                "Check the gateway status, the provider key, and the spend cap. "
                "Failed work refunds automatically."
            ),
        )
    ]


async def check_credit_invariants(
    session_factory: async_sessionmaker[AsyncSession],
) -> list[Alert]:
    """Alert when an account disagrees with the sum of its ledger movements."""
    ledger = (
        select(
            CreditLedgerEntry.user_id.label("user_id"),
            func.coalesce(func.sum(CreditLedgerEntry.delta_balance), 0).label("balance"),
            func.coalesce(func.sum(CreditLedgerEntry.delta_held), 0).label("held"),
        )
        .group_by(CreditLedgerEntry.user_id)
        .subquery()
    )
    async with session_scope(session_factory) as session:
        count = await session.scalar(
            select(func.count())
            .select_from(CreditAccount)
            .outerjoin(ledger, CreditAccount.user_id == ledger.c.user_id)
            .where(
                or_(
                    CreditAccount.balance != func.coalesce(ledger.c.balance, 0),
                    CreditAccount.held != func.coalesce(ledger.c.held, 0),
                )
            )
        )
    broken = int(count or 0)
    if broken == 0:
        return []
    return [
        Alert(
            code=AlertCode.CREDIT_INVARIANT_VIOLATION,
            severity="critical",
            summary=f"{broken} Credit account(s) disagree with the ledger.",
            detail=(
                "Stop spending and inspect `credit_accounts` against "
                "`credit_ledger_entries`. Correct with a compensating entry, never an update."
            ),
        )
    ]


async def collect_alerts(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> list[Alert]:
    """Evaluate every condition. Readiness short-circuits the database checks."""
    readiness = await check_readiness(engine)
    if readiness:
        return readiness
    alerts: list[Alert] = []
    alerts.extend(await check_credit_invariants(session_factory))
    alerts.extend(await check_queue_backlog(session_factory, settings))
    alerts.extend(await check_provider_errors(session_factory, settings))
    return alerts
