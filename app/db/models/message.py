"""Messages: the persisted turns of a chat.

A message is never hard-deleted on its own. Chats are soft-deleted, and deleting
a user (or, in a later demo reset, a chat) cascades to messages so history does
not outlive its owner. The assistant placeholder is inserted before the provider
is called and updated in place as the stream completes, fails, or is cancelled.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Text,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.ids import uuid7
from app.db.base import Base
from app.db.models.common import TimestampMixin, UpdatedAtMixin

MESSAGE_ROLES = ("system", "user", "assistant")
MESSAGE_STATUSES = ("complete", "streaming", "failed", "cancelled")

_ROLE_CHECK = "role IN ('" + "', '".join(MESSAGE_ROLES) + "')"
_STATUS_CHECK = "status IN ('" + "', '".join(MESSAGE_STATUSES) + "')"


class Message(TimestampMixin, UpdatedAtMixin, Base):
    """One turn in a chat, owned through its chat and its `user_id`."""

    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint(_ROLE_CHECK, name="role_valid"),
        CheckConstraint(_STATUS_CHECK, name="status_valid"),
        CheckConstraint(
            "prompt_tokens IS NULL OR prompt_tokens >= 0",
            name="prompt_tokens_non_negative",
        ),
        CheckConstraint(
            "completion_tokens IS NULL OR completion_tokens >= 0",
            name="completion_tokens_non_negative",
        ),
        CheckConstraint(
            "client_id IS NULL OR btrim(client_id) <> ''",
            name="client_id_not_blank",
        ),
        Index("ix_messages_chat_id_created_at", "chat_id", "created_at"),
        # A client-generated id identifies one user message in one chat. The
        # partial predicate keeps every assistant row (client_id null) out.
        Index(
            "uq_messages_chat_id_client_id",
            "chat_id",
            "client_id",
            unique=True,
            postgresql_where=text("client_id IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid7)
    chat_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("chats.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: The model that produced an assistant message; null on user/system turns.
    model_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("models.id", ondelete="RESTRICT"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, default="complete")
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: The mapped problem code stored when a stream fails.
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Client-generated idempotency marker for the user message.
    client_id: Mapped[str | None] = mapped_column(Text, nullable=True)
