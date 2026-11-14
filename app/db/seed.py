"""Seed data that must match what the frontend shows.

`PLAN_SEEDS` is the runtime source of truth for plan rows. The `0002` migration
inserts the same values into a fresh database; the test suite asserts the two
stay in sync.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Plan


@dataclass(frozen=True)
class PlanSeed:
    id: str
    name: str
    price_cents: int
    monthly_credits: int
    tagline: str
    features: tuple[str, ...]
    highlighted: bool
    sort_order: int


PLAN_SEEDS: tuple[PlanSeed, ...] = (
    PlanSeed(
        id="free",
        name="Free",
        price_cents=0,
        monthly_credits=50,
        tagline="Start creating for free",
        features=(
            "50 Credits every month",
            "Chat with the Nero AI assistant",
            "Export your creations",
        ),
        highlighted=False,
        sort_order=0,
    ),
    PlanSeed(
        id="pro",
        name="Pro",
        price_cents=1900,
        monthly_credits=1000,
        tagline="For daily creators",
        features=(
            "1,000 Credits every month",
            "Everything in Free",
            "Priority email support",
        ),
        highlighted=True,
        sort_order=1,
    ),
    PlanSeed(
        id="studio",
        name="Studio",
        price_cents=4900,
        monthly_credits=3000,
        tagline="For teams and heavy use",
        features=(
            "3,000 Credits every month",
            "Everything in Pro",
            "Priority support",
        ),
        highlighted=False,
        sort_order=2,
    ),
)


def plan_values() -> list[dict[str, Any]]:
    """Return the seed rows in insert-ready form."""
    return [
        {
            "id": plan.id,
            "name": plan.name,
            "price_cents": plan.price_cents,
            "monthly_credits": plan.monthly_credits,
            "tagline": plan.tagline,
            "features": list(plan.features),
            "highlighted": plan.highlighted,
            "sort_order": plan.sort_order,
        }
        for plan in PLAN_SEEDS
    ]


async def upsert_plans(session: AsyncSession) -> None:
    """Insert or refresh the plan catalog.

    Idempotent: rerunning updates existing rows instead of duplicating them, so a
    seed step can safely run on every deploy.
    """
    statement = insert(Plan).values(plan_values())
    statement = statement.on_conflict_do_update(
        index_elements=[Plan.id],
        set_={
            "name": statement.excluded.name,
            "price_cents": statement.excluded.price_cents,
            "monthly_credits": statement.excluded.monthly_credits,
            "tagline": statement.excluded.tagline,
            "features": statement.excluded.features,
            "highlighted": statement.excluded.highlighted,
            "sort_order": statement.excluded.sort_order,
        },
    )
    await session.execute(statement)
