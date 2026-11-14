"""User accounts."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import TIMESTAMP, CheckConstraint, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.core.ids import uuid7
from app.db.base import Base
from app.db.models.common import TimestampMixin, UpdatedAtMixin


class User(TimestampMixin, UpdatedAtMixin, Base):
    """A Nero AI account.

    Emails are stored normalized (lowercase) and the database rejects uppercase
    values, so the plain unique constraint is effectively case-insensitive.
    """

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("email = lower(email)", name="email_lowercase"),
        CheckConstraint(
            "char_length(display_name) BETWEEN 1 AND 80",
            name="display_name_length",
        ),
        CheckConstraint("role IN ('user', 'admin')", name="role_valid"),
        CheckConstraint("status IN ('active', 'disabled')", name="status_valid"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid7)
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False, default="user", server_default="user")
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="active", server_default="active"
    )
    email_verified_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    last_login_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
