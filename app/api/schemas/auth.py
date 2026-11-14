"""Request and response schemas for auth endpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, PlainSerializer
from pydantic.alias_generators import to_camel


def _serialize_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


UtcDatetime = Annotated[datetime, PlainSerializer(_serialize_utc, return_type=str)]


class CamelModel(BaseModel):
    """camelCase at the HTTP boundary, snake_case in Python."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class CamelRequest(CamelModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )


class SignupRequest(CamelRequest):
    email: EmailStr
    password: str
    display_name: str | None = Field(default=None, max_length=80)


class LoginRequest(CamelRequest):
    email: EmailStr
    password: str


class UserResponse(CamelModel):
    id: UUID
    email: str
    display_name: str
    role: str
    created_at: UtcDatetime


class PlanSummary(CamelModel):
    id: str
    name: str
    monthly_credits: int


class CreditsSummary(CamelModel):
    balance: int
    held: int
    available: int
    allowance: int
    period_start: UtcDatetime
    period_end: UtcDatetime


class MeResponse(CamelModel):
    user: UserResponse
    plan: PlanSummary
    credits: CreditsSummary
