"""Shared column definitions for models."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import TIMESTAMP, func
from sqlalchemy.orm import Mapped, mapped_column


def utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(tz=UTC)


class TimestampMixin:
    """`created_at` for append-style rows, defaulted in Python and PostgreSQL."""

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=func.now(),
    )


class UpdatedAtMixin:
    """`updated_at` that refreshes on every ORM update."""

    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
    )
