"""Chats: one persisted conversation owned by one user.

Messages live in the sibling `messages` table; a chat exists first so the client
can pick a model and the first user turn can derive its title. A chat is
soft-deleted, never removed, so a client that reconnects cannot resurrect history
it already hid from itself.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    TIMESTAMP,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Text,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.ids import uuid7
from app.db.base import Base
from app.db.models.common import TimestampMixin, UpdatedAtMixin

DEFAULT_TITLE = "New chat"
MAX_TITLE_LENGTH = 80
#: Longest prefix of the first user message that becomes a title.
MAX_TITLE_SOURCE_LENGTH = 60


class Chat(TimestampMixin, UpdatedAtMixin, Base):
    """A conversation. Every read is scoped by `user_id`."""

    __tablename__ = "chats"
    __table_args__ = (
        CheckConstraint(
            f"char_length(title) BETWEEN 1 AND {MAX_TITLE_LENGTH}",
            name="title_length",
        ),
        CheckConstraint("btrim(title) <> ''", name="title_not_blank"),
        # Partial: keeps the listing and its keyset cursor covered while
        # soft-deleted rows stay out of the index. A backward scan of the
        # ascending columns serves `ORDER BY updated_at DESC, id DESC`.
        Index(
            "ix_chats_user_id_updated_at",
            "user_id",
            "updated_at",
            "id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid7)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(Text, nullable=False, default=DEFAULT_TITLE)
    #: Whether the caller chose the title. B3.3 derives one from the first user
    #: message only while this is false, so an explicit rename survives.
    title_is_custom: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    model_id: Mapped[str] = mapped_column(
        Text,
        # The catalog seed refuses to delete a referenced model, so a chat can
        # never point at a model that is no longer offered.
        ForeignKey("models.id", ondelete="RESTRICT"),
        nullable=False,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
