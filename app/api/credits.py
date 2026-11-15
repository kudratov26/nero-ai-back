"""Credit endpoints: summary, paginated ledger, and the simulated plan change.

All three are caller-scoped. Reads map the account the auth dependency already
loaded; the writes go through `app.domain.billing`.
"""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_app_settings, get_current_auth, get_session_factory
from app.api.problems import PROBLEM_MEDIA_TYPE, ApiError, ErrorCode, ProblemDetail
from app.api.responses import credits_summary, me_response
from app.api.schemas.auth import CreditsSummary, MeResponse
from app.api.schemas.credits import (
    LedgerEntryResponse,
    LedgerKind,
    LedgerPage,
    LedgerRefType,
    PlanChangeRequest,
)
from app.core.config import Settings
from app.db.models import CreditLedgerEntry
from app.domain import billing
from app.domain.auth import AuthContext

router = APIRouter(prefix="/v1", tags=["credits"])

PLAN_CHANGES_DISABLED_DETAIL = "Plan changes are not available."


def _ledger_entry(entry: CreditLedgerEntry) -> LedgerEntryResponse:
    return LedgerEntryResponse(
        id=entry.id,
        # The database constrains both columns to the documented sets.
        kind=cast(LedgerKind, entry.kind),
        delta_balance=entry.delta_balance,
        delta_held=entry.delta_held,
        balance_after=entry.balance_after,
        held_after=entry.held_after,
        reason=entry.reason,
        ref_type=cast(LedgerRefType | None, entry.ref_type),
        ref_id=entry.ref_id,
        created_at=entry.created_at,
    )


@router.get(
    "/credits",
    response_model=CreditsSummary,
    summary="Get the Credit summary",
    description="Returns the caller's balance, held, available, and allowance.",
)
async def credits(
    context: Annotated[AuthContext, Depends(get_current_auth)],
) -> CreditsSummary:
    return credits_summary(context.account)


@router.get(
    "/credits/ledger",
    response_model=LedgerPage,
    summary="List the Credit ledger",
    description=(
        "Returns the caller's ledger, newest first, as an opaque-cursor page. "
        "Entries are immutable; corrections appear as new entries."
    ),
)
async def ledger(
    context: Annotated[AuthContext, Depends(get_current_auth)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    cursor: Annotated[str | None, Query(description="Opaque cursor from `nextCursor`.")] = None,
    limit: Annotated[
        int,
        Query(ge=1, le=billing.LEDGER_MAX_PAGE_SIZE, description="Items per page."),
    ] = billing.LEDGER_PAGE_SIZE,
) -> LedgerPage:
    entries, next_cursor = await billing.list_ledger(
        session_factory,
        user_id=context.user.id,
        cursor=cursor,
        limit=limit,
    )
    return LedgerPage(items=[_ledger_entry(entry) for entry in entries], next_cursor=next_cursor)


@router.post(
    "/account/plan",
    response_model=MeResponse,
    summary="Change the plan",
    description=(
        "Simulated plan change: refills the account to the new allowance and starts a "
        "new period. Disabled unless `ALLOW_SIMULATED_PLAN_CHANGES` is enabled; "
        "rejected with 409 while Credits are held."
    ),
    responses={
        404: {
            "description": "The plan is unknown or plan changes are disabled.",
            "content": {PROBLEM_MEDIA_TYPE: {"schema": ProblemDetail.model_json_schema()}},
        },
        409: {
            "description": "Credits are held for in-flight work.",
            "content": {PROBLEM_MEDIA_TYPE: {"schema": ProblemDetail.model_json_schema()}},
        },
    },
)
async def change_plan(
    body: PlanChangeRequest,
    context: Annotated[AuthContext, Depends(get_current_auth)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> MeResponse:
    if not settings.simulated_plan_changes_allowed:
        raise ApiError(ErrorCode.NOT_FOUND, 404, detail=PLAN_CHANGES_DISABLED_DETAIL)
    user, plan, account = await billing.apply_plan(
        session_factory,
        user_id=context.user.id,
        plan_id=body.plan_id,
    )
    return me_response(AuthContext(user=user, session=context.session, plan=plan, account=account))
