"""The Credit ledger: `reserve`, `commit`, `refund`, `grant`, and `reset`.

Every operation locks the account row (`SELECT ... FOR UPDATE`), updates the
balance and held amounts, and appends an immutable ledger entry in the same
transaction. Callers own the transaction: a use case opens `session_scope`, then
calls these functions so Credits, domain rows, and ledger entries commit or roll
back together.

`available = balance - held` and it can never go negative. The database enforces
that with check constraints; this module is the only writer, so it checks first
and returns a clean `409 insufficient_credits` instead of a constraint failure.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.problems import ApiError, ErrorCode
from app.db.models import CreditAccount, CreditLedgerEntry, Plan

RESERVE = "reserve"
COMMIT = "commit"
REFUND = "refund"
GRANT = "grant"
RESET = "reset"
PLAN_CHANGE = "plan_change"

#: Length of a billing period, in days. Signup and every plan change start one.
PLAN_PERIOD_DAYS = 30


def available(account: CreditAccount) -> int:
    """Credits the account can spend right now."""
    return account.balance - account.held


def _require_positive(amount: int) -> None:
    # Amounts are server-owned, so a non-positive one is a programming error,
    # not a client mistake.
    if amount <= 0:
        raise ValueError(f"amount must be positive, got {amount}")


def _insufficient_credits(amount: int, remaining: int) -> ApiError:
    return ApiError(
        ErrorCode.INSUFFICIENT_CREDITS,
        409,
        detail=f"This run needs {amount} Credits; {remaining} are available.",
    )


async def _lock_account(session: AsyncSession, user_id: UUID) -> CreditAccount:
    """Lock the account row so concurrent operations serialize on it."""
    account = await session.scalar(
        select(CreditAccount).where(CreditAccount.user_id == user_id).with_for_update()
    )
    if account is None:
        raise RuntimeError(f"no credit account for user {user_id}")
    return account


async def _append(
    session: AsyncSession,
    account: CreditAccount,
    *,
    kind: str,
    delta_balance: int,
    delta_held: int,
    reason: str,
    ref_type: str | None,
    ref_id: UUID | None,
) -> CreditLedgerEntry:
    """Apply deltas to the locked account and record the matching entry."""
    account.balance += delta_balance
    account.held += delta_held
    entry = CreditLedgerEntry(
        user_id=account.user_id,
        kind=kind,
        delta_balance=delta_balance,
        delta_held=delta_held,
        balance_after=account.balance,
        held_after=account.held,
        reason=reason,
        ref_type=ref_type,
        ref_id=ref_id,
    )
    session.add(entry)
    await session.flush()
    return entry


async def reserve(
    session: AsyncSession,
    *,
    user_id: UUID,
    amount: int,
    reason: str,
    ref_type: str | None = None,
    ref_id: UUID | None = None,
) -> CreditAccount:
    """Move `amount` Credits from available to held before provider work.

    Raises `409 insufficient_credits` when the account cannot cover it.
    """
    _require_positive(amount)
    account = await _lock_account(session, user_id)
    remaining = available(account)
    if remaining < amount:
        raise _insufficient_credits(amount, remaining)
    await _append(
        session,
        account,
        kind=RESERVE,
        delta_balance=0,
        delta_held=amount,
        reason=reason,
        ref_type=ref_type,
        ref_id=ref_id,
    )
    return account


async def commit(
    session: AsyncSession,
    *,
    user_id: UUID,
    amount: int,
    reason: str,
    ref_type: str | None = None,
    ref_id: UUID | None = None,
) -> CreditAccount:
    """Consume a held reservation after successful work."""
    _require_positive(amount)
    account = await _lock_account(session, user_id)
    if account.held < amount:
        raise ValueError(f"cannot commit {amount} Credits; only {account.held} are held")
    await _append(
        session,
        account,
        kind=COMMIT,
        delta_balance=-amount,
        delta_held=-amount,
        reason=reason,
        ref_type=ref_type,
        ref_id=ref_id,
    )
    return account


async def refund(
    session: AsyncSession,
    *,
    user_id: UUID,
    amount: int,
    reason: str,
    ref_type: str | None = None,
    ref_id: UUID | None = None,
) -> CreditAccount:
    """Return a held reservation to available after failure, cancel, or timeout."""
    _require_positive(amount)
    account = await _lock_account(session, user_id)
    if account.held < amount:
        raise ValueError(f"cannot refund {amount} Credits; only {account.held} are held")
    await _append(
        session,
        account,
        kind=REFUND,
        delta_balance=0,
        delta_held=-amount,
        reason=reason,
        ref_type=ref_type,
        ref_id=ref_id,
    )
    return account


async def grant(
    session: AsyncSession,
    *,
    user_id: UUID,
    amount: int,
    reason: str,
    ref_type: str | None = None,
    ref_id: UUID | None = None,
) -> CreditAccount:
    """Add Credits that were not previously held (signup, an admin adjustment)."""
    _require_positive(amount)
    account = await _lock_account(session, user_id)
    await _append(
        session,
        account,
        kind=GRANT,
        delta_balance=amount,
        delta_held=0,
        reason=reason,
        ref_type=ref_type,
        ref_id=ref_id,
    )
    return account


async def reset(
    session: AsyncSession,
    *,
    user_id: UUID,
    reason: str,
    ref_type: str | None = None,
    ref_id: UUID | None = None,
) -> CreditAccount:
    """Refund any hold and restore the account to the current plan allowance.

    Used by demo reset. When nothing changed (already at the allowance with no
    hold) no ledger entry is written, so repeating the call is a true no-op.
    """
    account = await _lock_account(session, user_id)
    delta_balance = account.allowance - account.balance
    delta_held = -account.held
    if delta_balance == 0 and delta_held == 0:
        return account
    await _append(
        session,
        account,
        kind=RESET,
        delta_balance=delta_balance,
        delta_held=delta_held,
        reason=reason,
        ref_type=ref_type,
        ref_id=ref_id,
    )
    return account


async def change_plan(
    session: AsyncSession,
    *,
    user_id: UUID,
    plan: Plan,
    now: datetime,
) -> CreditAccount:
    """Switch the account to `plan`, refill it, and start a new period.

    Rejected with `409 conflict` while Credits are held, so a plan change can never
    strand an in-flight reservation. Refills `balance` to the new allowance.
    """
    account = await _lock_account(session, user_id)
    if account.held > 0:
        raise ApiError(
            ErrorCode.CONFLICT,
            409,
            detail="Wait for in-flight work to finish before changing plans.",
        )
    delta_balance = plan.monthly_credits - account.balance
    account.plan_id = plan.id
    account.allowance = plan.monthly_credits
    account.period_start = now
    account.period_end = now + timedelta(days=PLAN_PERIOD_DAYS)
    await _append(
        session,
        account,
        kind=PLAN_CHANGE,
        delta_balance=delta_balance,
        delta_held=0,
        reason=f"Changed plan to {plan.name}",
        ref_type="plan",
        ref_id=None,
    )
    return account
