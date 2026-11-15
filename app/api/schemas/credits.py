"""Schemas for credits, the ledger, and the simulated plan change."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import Field

from app.api.schemas.base import CamelModel, CamelRequest, UtcDatetime

LedgerKind = Literal[
    "grant",
    "reserve",
    "commit",
    "refund",
    "plan_change",
    "adjustment",
    "reset",
]
LedgerRefType = Literal["chat_message", "generation", "plan", "demo_reset"]


class PlanChangeRequest(CamelRequest):
    plan_id: str = Field(min_length=1, max_length=40)


class LedgerEntryResponse(CamelModel):
    """One immutable Credit movement. The ledger is append-only."""

    id: UUID
    kind: LedgerKind
    delta_balance: int
    delta_held: int
    balance_after: int
    held_after: int
    reason: str
    ref_type: LedgerRefType | None
    ref_id: UUID | None
    created_at: UtcDatetime


class LedgerPage(CamelModel):
    items: list[LedgerEntryResponse]
    next_cursor: str | None
