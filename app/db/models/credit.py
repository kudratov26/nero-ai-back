"""Credit account and append-only ledger.

The ledger service arrives in Phase 2; these tables exist now so signup can create
the account and its opening grant in the same transaction as the user.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    TIMESTAMP,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Text,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.ids import uuid7
from app.db.base import Base
from app.db.models.common import UpdatedAtMixin

LEDGER_KINDS = (
    "grant",
    "reserve",
    "commit",
    "refund",
    "plan_change",
    "adjustment",
    "reset",
)

LEDGER_REF_TYPES = (
    "chat_message",
    "generation",
    "plan",
    "demo_reset",
)

_KIND_CHECK = "kind IN ('" + "', '".join(LEDGER_KINDS) + "')"
_REF_TYPE_CHECK = "ref_type IS NULL OR ref_type IN ('" + "', '".join(LEDGER_REF_TYPES) + "')"


class CreditAccount(UpdatedAtMixin, Base):
    """One balance row per user. All changes lock this row (`SELECT ... FOR UPDATE`)."""

    __tablename__ = "credit_accounts"
    __table_args__ = (
        CheckConstraint("balance >= 0", name="balance_non_negative"),
        CheckConstraint("held >= 0", name="held_non_negative"),
        CheckConstraint("allowance >= 0", name="allowance_non_negative"),
        CheckConstraint("balance >= held", name="available_non_negative"),
        CheckConstraint("period_end > period_start", name="period_valid"),
    )

    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    plan_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("plans.id", ondelete="RESTRICT"),
        nullable=False,
    )
    balance: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    held: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    allowance: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    period_start: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)


class CreditLedgerEntry(Base):
    """An immutable record of every Credit movement.

    Rows are never updated or deleted by the application; corrections are new
    compensating entries.
    """

    __tablename__ = "credit_ledger_entries"
    __table_args__ = (
        CheckConstraint(_KIND_CHECK, name="kind_valid"),
        CheckConstraint(_REF_TYPE_CHECK, name="ref_type_valid"),
        CheckConstraint("balance_after >= 0", name="balance_after_non_negative"),
        CheckConstraint("held_after >= 0", name="held_after_non_negative"),
        CheckConstraint("balance_after >= held_after", name="available_after_non_negative"),
        Index("ix_credit_ledger_entries_user_created", "user_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid7)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    delta_balance: Mapped[int] = mapped_column(Integer, nullable=False)
    delta_held: Mapped[int] = mapped_column(Integer, nullable=False)
    balance_after: Mapped[int] = mapped_column(Integer, nullable=False)
    held_after: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    ref_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    ref_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
