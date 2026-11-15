"""Response schemas for the public catalog."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from app.api.schemas.base import CamelModel

Modality = Literal["chat", "image", "video"]


class PlanResponse(CamelModel):
    id: str
    name: str
    price_cents: int
    monthly_credits: int
    tagline: str
    features: list[str]
    highlighted: bool
    sort_order: int


class ModelResponse(CamelModel):
    """A selectable model. Costs are server-owned; the client never sends one."""

    id: str
    name: str
    provider: str
    modality: Modality
    description: str
    badge: str | None
    credit_cost: int
    # The generic generator would render this as `creditCost10S`; pin the natural
    # spelling so the JSON field stays `creditCost10s`.
    credit_cost_10s: int | None = Field(default=None, alias="creditCost10s")
    capabilities: dict[str, Any]
    context_window: int | None
    sort_order: int
