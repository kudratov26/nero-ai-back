"""Sweep reservations whose owner never reached a terminal state.

A Credit hold is meant to be short-lived: a Generation settles through the worker,
and a chat message settles on completion, failure, or disconnect. If the process
that owned the work dies before it can settle, the hold would otherwise stay held
forever, shrinking the account's available balance with nothing running.

The sweeper is the safety net. It finds a Generation stuck `queued`, a Generation
still `running` with a lock older than the TTL, or an assistant message stuck
`streaming`, marks it `failed`, and refunds the amount still held for that
reference. The amount comes from the ledger (`sum(delta_held)` for the reference),
not from a current price: it is zero once the reservation was committed or
refunded, so a second sweep cannot refund twice. A `running` job whose lock was
refreshed recently is left alone, because a worker still owns it.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

import structlog
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.problems import ErrorCode
from app.db.models import CreditLedgerEntry, Generation, Message
from app.db.models.common import utcnow
from app.db.session import session_scope
from app.domain import credits

log = structlog.get_logger(__name__)

#: How many stranded rows one sweep settles. The sweep runs between worker polls,
#: so a large backlog is drained over several passes instead of one long
#: transaction that holds many account locks.
HOLD_SWEEP_BATCH = 100

GENERATION_REF = "generation"
MESSAGE_REF = "chat_message"

EXPIRED_GENERATION_REASON = "Expired generation"
EXPIRED_MESSAGE_REASON = "Expired chat message"


async def _held_for(session: AsyncSession, *, ref_type: str, ref_id: UUID) -> int:
    """The Credits still held for one reference, from the append-only ledger."""
    total = await session.scalar(
        select(func.coalesce(func.sum(CreditLedgerEntry.delta_held), 0)).where(
            CreditLedgerEntry.ref_type == ref_type,
            CreditLedgerEntry.ref_id == ref_id,
        )
    )
    return int(total or 0)


async def _sweep_generations(session: AsyncSession, cutoff: datetime) -> int:
    """Fail and refund Generations whose hold outlived its TTL.

    A `queued` row is stranded from the moment its hold was created. A `running`
    row is only stranded once its lock is older than the cutoff too: a worker may
    have picked up an old job just before the sweep, and failing it out from
    under that worker would waste the render and refund a job that was about to
    finish.
    """
    stranded = or_(
        (Generation.status == "queued") & (Generation.created_at < cutoff),
        (Generation.status == "running")
        & (func.coalesce(Generation.locked_at, Generation.created_at) < cutoff),
    )
    ids = list(
        await session.scalars(
            select(Generation.id)
            .where(stranded)
            .order_by(Generation.created_at, Generation.id)
            .limit(HOLD_SWEEP_BATCH)
        )
    )
    swept = 0
    for generation_id in ids:
        row = await session.get(Generation, generation_id, with_for_update=True)
        if row is None or row.status not in ("queued", "running"):
            # A worker settled it between the scan and the lock.
            continue
        stale_lock = (row.locked_at or row.created_at) < cutoff
        if row.status == "running" and not stale_lock:
            # Claimed after the scan; the worker owns it now.
            continue
        amount = await _held_for(session, ref_type=GENERATION_REF, ref_id=row.id)
        if amount > 0:
            await credits.refund(
                session,
                user_id=row.user_id,
                amount=amount,
                reason=EXPIRED_GENERATION_REASON,
                ref_type=GENERATION_REF,
                ref_id=row.id,
            )
        row.status = "failed"
        row.error_code = ErrorCode.HOLD_EXPIRED.value
        row.locked_by = None
        row.locked_at = None
        row.finished_at = utcnow()
        swept += 1
        log.warning(
            "hold.expired",
            kind=GENERATION_REF,
            generation_id=str(row.id),
            refunded=amount,
        )
    return swept


async def _sweep_messages(session: AsyncSession, cutoff: datetime) -> int:
    """Fail and refund assistant messages left `streaming` past the TTL."""
    ids = list(
        await session.scalars(
            select(Message.id)
            .where(
                Message.role == "assistant",
                Message.status == "streaming",
                Message.updated_at < cutoff,
            )
            .order_by(Message.updated_at, Message.id)
            .limit(HOLD_SWEEP_BATCH)
        )
    )
    swept = 0
    for message_id in ids:
        row = await session.get(Message, message_id, with_for_update=True)
        if row is None or row.status != "streaming":
            continue
        amount = await _held_for(session, ref_type=MESSAGE_REF, ref_id=row.id)
        if amount > 0:
            await credits.refund(
                session,
                user_id=row.user_id,
                amount=amount,
                reason=EXPIRED_MESSAGE_REASON,
                ref_type=MESSAGE_REF,
                ref_id=row.id,
            )
        row.status = "failed"
        row.error_code = ErrorCode.HOLD_EXPIRED.value
        row.updated_at = utcnow()
        swept += 1
        log.warning(
            "hold.expired",
            kind=MESSAGE_REF,
            message_id=str(row.id),
            refunded=amount,
        )
    return swept


async def sweep_expired_holds(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    older_than: timedelta,
    now: datetime | None = None,
) -> int:
    """Settle every stranded hold older than `older_than`. Returns the count.

    Generations and messages are swept in separate transactions, so one blocked
    row cannot hold up the other kind. Each row is re-checked under its lock, so a
    job that settled while the sweep was scanning is left alone.
    """
    cutoff = (now or utcnow()) - older_than
    async with session_scope(session_factory) as session:
        generations = await _sweep_generations(session, cutoff)
    async with session_scope(session_factory) as session:
        messages = await _sweep_messages(session, cutoff)
    total = generations + messages
    if total:
        log.warning(
            "holds.swept",
            generations=generations,
            messages=messages,
        )
    return total
