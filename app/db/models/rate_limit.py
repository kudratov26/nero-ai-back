"""Persistent fixed-window counters for endpoint rate limits.

One row per throttle key (for example, an IP or an IP plus email). The row is
updated with an atomic upsert, so concurrent requests and multiple API
processes share the same budget; a restart does not reset an attacker's window.

Expired rows are pruned at startup. They are bounded by the number of distinct
throttle keys, and the index on `window_end` makes the prune a cheap range
delete.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import TIMESTAMP, CheckConstraint, Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class RateLimitWindow(Base):
    """A fixed window of attempts for one throttle key."""

    __tablename__ = "rate_limit_windows"
    __table_args__ = (
        CheckConstraint("btrim(key) <> ''", name="key_not_blank"),
        CheckConstraint("count >= 0", name="count_non_negative"),
        Index("ix_rate_limit_windows_window_end", "window_end"),
    )

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    window_end: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False)
