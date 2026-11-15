"""Idempotency keys for create operations.

A create endpoint records the outcome of its first request under `(user_id, key)`.
A retry with the same key and body replays the recorded resource instead of
creating a second one; the same key with a different body is a conflict.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.common import TimestampMixin

REQUEST_HASH_LENGTH = 64
MAX_KEY_LENGTH = 255


class IdempotencyKey(TimestampMixin, Base):
    """One recorded create outcome per `(user_id, key)`.

    `resource_id` is deliberately not a foreign key: it points at whatever the
    operation created (a Chat, a Generation, a Message), so the table stays
    usable for every create route.
    """

    __tablename__ = "idempotency_keys"
    __table_args__ = (
        CheckConstraint("btrim(key) <> ''", name="key_not_blank"),
        CheckConstraint(f"char_length(key) <= {MAX_KEY_LENGTH}", name="key_length"),
        CheckConstraint("btrim(operation) <> ''", name="operation_not_blank"),
        CheckConstraint(
            f"char_length(request_hash) = {REQUEST_HASH_LENGTH}", name="request_hash_sha256"
        ),
        Index("ix_idempotency_keys_created_at", "created_at"),
    )

    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    operation: Mapped[str] = mapped_column(Text, nullable=False)
    request_hash: Mapped[str] = mapped_column(Text, nullable=False)
    resource_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
