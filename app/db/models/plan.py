"""Subscription plans shown on the pricing page."""

from __future__ import annotations

from sqlalchemy import Boolean, CheckConstraint, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Plan(Base):
    """A seeded plan. The catalog is server-authoritative; clients never set prices."""

    __tablename__ = "plans"
    __table_args__ = (
        CheckConstraint("price_cents >= 0", name="price_non_negative"),
        CheckConstraint("monthly_credits >= 0", name="credits_non_negative"),
        CheckConstraint("sort_order >= 0", name="sort_order_non_negative"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    monthly_credits: Mapped[int] = mapped_column(Integer, nullable=False)
    tagline: Mapped[str] = mapped_column(Text, nullable=False)
    features: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    highlighted: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)
