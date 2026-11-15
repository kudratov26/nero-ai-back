"""Request, history, and SSE payload schemas for chat messages."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator

from app.api.schemas.base import CamelModel, CamelRequest, UtcDatetime

MessageRole = Literal["system", "user", "assistant"]
MessageStatus = Literal["complete", "streaming", "failed", "cancelled"]

#: Matches the documented prompt limit.
MAX_CONTENT_LENGTH = 4000
MAX_CLIENT_ID_LENGTH = 120


class SendMessageRequest(CamelRequest):
    content: str = Field(min_length=1, max_length=MAX_CONTENT_LENGTH)
    model_id: str | None = Field(default=None, min_length=1, max_length=120)
    client_id: str | None = Field(default=None, max_length=MAX_CLIENT_ID_LENGTH)

    @field_validator("content")
    @classmethod
    def _require_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content cannot be blank")
        return value

    @field_validator("client_id")
    @classmethod
    def _normalize_client_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("clientId cannot be blank")
        return trimmed


class MessageResponse(CamelModel):
    id: UUID
    role: MessageRole
    content: str
    model_id: str | None
    status: MessageStatus
    prompt_tokens: int | None
    completion_tokens: int | None
    error_code: str | None
    client_id: str | None
    created_at: UtcDatetime
    updated_at: UtcDatetime


class MessagePage(CamelModel):
    items: list[MessageResponse]
    next_cursor: str | None


class StartedPayload(CamelModel):
    user_message_id: UUID
    assistant_message_id: UUID
    model_id: str


class DeltaPayload(CamelModel):
    message_id: UUID
    delta: str


class UsagePayload(CamelModel):
    prompt_tokens: int | None
    completion_tokens: int | None


class CreditsPayload(CamelModel):
    balance: int
    held: int
    available: int
    charged: int


class CompletedPayload(CamelModel):
    message_id: UUID
    content: str
    usage: UsagePayload
    credits: CreditsPayload


class FailedPayload(CamelModel):
    message_id: UUID
    code: str
    detail: str
    refunded: bool
