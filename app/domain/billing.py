"""Use cases behind the credit and plan endpoints.

These own their transactions and delegate the actual Credit movement to
`app.domain.credits`, so reads and writes stay consistent with the ledger.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.problems import ApiError, ErrorCode
from app.core.cursor import InvalidCursorError, decode_cursor, encode_cursor
from app.db.models import CreditAccount, CreditLedgerEntry, Plan, User
from app.db.session import session_scope
from app.domain import credits

LEDGER_PAGE_SIZE = 20
LEDGER_MAX_PAGE_SIZE = 100

PLAN_NOT_FOUND_DETAIL = "That plan does not exist."
INVALID_CURSOR_DETAIL = "The cursor is not valid."
UNAUTHORIZED_DETAIL = "Authentication required."


def _cursor_for(entry: CreditLedgerEntry) -> str:
    return encode_cursor({"createdAt": _iso(entry.created_at), "id": str(entry.id)})


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _decode_ledger_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        data = decode_cursor(cursor)
        created_at = datetime.fromisoformat(str(data["createdAt"]))
        entry_id = UUID(str(data["id"]))
    except (InvalidCursorError, KeyError, ValueError) as exc:
        raise ApiError(ErrorCode.INVALID_REQUEST, 400, detail=INVALID_CURSOR_DETAIL) from exc
    if created_at.tzinfo is None:
        raise ApiError(ErrorCode.INVALID_REQUEST, 400, detail=INVALID_CURSOR_DETAIL)
    return created_at, entry_id


async def list_ledger(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: UUID,
    cursor: str | None,
    limit: int,
) -> tuple[list[CreditLedgerEntry], str | None]:
    """Return one page of the caller's ledger, newest first.

    Keyset pagination on `(created_at, id)` keeps pages stable even when entries
    are appended between requests.
    """
    statement = (
        select(CreditLedgerEntry)
        .where(CreditLedgerEntry.user_id == user_id)
        .order_by(CreditLedgerEntry.created_at.desc(), CreditLedgerEntry.id.desc())
        .limit(limit + 1)
    )
    if cursor is not None:
        after = _decode_ledger_cursor(cursor)
        statement = statement.where(
            tuple_(CreditLedgerEntry.created_at, CreditLedgerEntry.id) < after
        )

    async with session_scope(session_factory) as session:
        rows = list(await session.scalars(statement))

    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = _cursor_for(page[-1]) if has_more and page else None
    return page, next_cursor


async def apply_plan(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: UUID,
    plan_id: str,
) -> tuple[User, Plan, CreditAccount]:
    """Switch the caller to `plan`. Returns the refreshed user, plan, and account."""
    now = datetime.now(tz=UTC)
    async with session_scope(session_factory) as session:
        plan = await session.get(Plan, plan_id)
        if plan is None:
            raise ApiError(ErrorCode.NOT_FOUND, 404, detail=PLAN_NOT_FOUND_DETAIL)
        account = await credits.change_plan(session, user_id=user_id, plan=plan, now=now)
        user = await session.get(User, user_id)
        if user is None:
            raise ApiError(ErrorCode.UNAUTHORIZED, 401, detail=UNAUTHORIZED_DETAIL)
        return user, plan, account
