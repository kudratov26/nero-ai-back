"""Request and response schemas for auth endpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, PlainSerializer, field_validator
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


class UpdateProfileRequest(CamelRequest):
    display_name: str = Field(min_length=1, max_length=80)

    @field_validator("display_name")
    @classmethod
    def _require_visible_name(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("display name cannot be blank")
        return trimmed


class PasswordChangeRequest(CamelRequest):
    current_password: str
    new_password: str


class UserResponse(CamelModel):
    id: UUID
    email: str
    display_name: str
    role: str
    created_at: UtcDatetime


class SessionSummary(CamelModel):
    id: UUID
    user_agent: str | None
    ip: str | None
    created_at: UtcDatetime
    last_used_at: UtcDatetime
    expires_at: UtcDatetime
    current: bool


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
