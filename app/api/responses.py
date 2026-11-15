"""Response mapping shared by more than one router.

Kept out of the routers so `MeResponse` and its Credit summary are built the same
way wherever they are returned.
"""

from __future__ import annotations

from app.api.schemas.auth import CreditsSummary, MeResponse, PlanSummary, UserResponse
from app.db.models import CreditAccount, User
from app.domain import credits as credits_service
from app.domain.auth import AuthContext


def user_response(user: User) -> UserResponse:
    return UserResponse(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        created_at=user.created_at,
    )


def credits_summary(account: CreditAccount) -> CreditsSummary:
    return CreditsSummary(
        balance=account.balance,
        held=account.held,
        available=credits_service.available(account),
        allowance=account.allowance,
        period_start=account.period_start,
        period_end=account.period_end,
    )


def me_response(context: AuthContext) -> MeResponse:
    return MeResponse(
        user=user_response(context.user),
        plan=PlanSummary(
            id=context.plan.id,
            name=context.plan.name,
            monthly_credits=context.plan.monthly_credits,
        ),
        credits=credits_summary(context.account),
    )
