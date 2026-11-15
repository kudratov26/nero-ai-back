"""Schemas for chat CRUD.

A listing row and a full chat carry the same fields today: the list renders a
model badge and a relative timestamp, and a read returns the same object the
create and rename responses do.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import Field, field_validator

from app.api.schemas.base import CamelModel, CamelRequest, UtcDatetime


def _title(value: str) -> str:
    """Trim a title and reject a blank one, matching the database constraint."""
    trimmed = value.strip()
    if not trimmed:
        raise ValueError("title cannot be blank")
    return trimmed


class CreateChatRequest(CamelRequest):
    model_id: str = Field(min_length=1, max_length=120)
    title: str | None = Field(default=None, max_length=80)

    @field_validator("title")
    @classmethod
    def _validate_title(cls, value: str | None) -> str | None:
        return None if value is None else _title(value)


class UpdateChatRequest(CamelRequest):
    title: str = Field(min_length=1, max_length=80)

    @field_validator("title")
    @classmethod
    def _validate_title(cls, value: str) -> str:
        return _title(value)


class ChatResponse(CamelModel):
    id: UUID
    title: str
    model_id: str
    created_at: UtcDatetime
    updated_at: UtcDatetime


class ChatSummary(ChatResponse):
    """A listing row. Same shape as a chat read, so one mapper serves both."""


class ChatPage(CamelModel):
    items: list[ChatSummary]
    next_cursor: str | None
