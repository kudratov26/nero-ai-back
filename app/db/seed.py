"""Seed data that must match what the frontend shows.

`PLAN_SEEDS` is the runtime source of truth for plan rows. The `0002` migration
inserts the same values into a fresh database; the test suite asserts the two
stay in sync.

The model catalog is seeded at runtime instead of in a migration, because a model's
`available` flag depends on configuration (`AI_BASE_URL`, `AI_API_KEY`,
`GENERATION_MODE`). Run `python -m app.cli seed` after `alembic upgrade head`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import Model, Plan

#: Provider name for the deterministic in-process Image/Video providers.
MOCK_PROVIDER = "Demo"

CHAT_MODEL_NAME = "DeepSeek V4.1 Flash"
CHAT_MODEL_DESCRIPTION = "Flagship assistant for reasoning, writing, and code."
CHAT_MODEL_CAPABILITIES: dict[str, Any] = {
    "streaming": True,
    "vision": False,
    "maxOutputTokens": 8192,
}
CHAT_MODEL_CONTEXT_WINDOW = 128_000
CHAT_MESSAGE_CREDITS = 1

IMAGE_MODEL_ID = "image-demo"
IMAGE_MODEL_NAME = "Image Demo"
IMAGE_MODEL_DESCRIPTION = "Deterministic placeholder images for demos and tests."
IMAGE_MODEL_CAPABILITIES: dict[str, Any] = {
    "streaming": False,
    "vision": False,
    "aspectRatios": ["1:1", "4:5", "16:9"],
    "maxCount": 4,
}
IMAGE_CREDITS = 5

VIDEO_MODEL_ID = "video-demo"
VIDEO_MODEL_NAME = "Video Demo"
VIDEO_MODEL_DESCRIPTION = "Deterministic placeholder clips for demos and tests."
VIDEO_MODEL_CAPABILITIES: dict[str, Any] = {
    "streaming": False,
    "vision": False,
    "aspectRatios": ["16:9", "9:16"],
    "durations": [5, 10],
}
VIDEO_CREDITS = 15
VIDEO_CREDITS_10S = 30


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


def model_values(settings: Settings) -> list[dict[str, Any]]:
    """Catalog rows for the current configuration, in insert-ready form.

    The chat model is advertised only when the gateway is configured. The mock
    Image and Video models exist always but are advertised only while
    `GENERATION_MODE=mock`, so a deployment without a real image adapter never
    offers one.
    """
    chat_available = settings.chat_provider_configured
    mock_available = settings.generation_mode == "mock"
    return [
        {
            "id": settings.ai_chat_model,
            "name": CHAT_MODEL_NAME,
            "provider": "Coarena",
            "provider_model_id": settings.ai_chat_model,
            "modality": "chat",
            "description": CHAT_MODEL_DESCRIPTION,
            "badge": "New",
            "credit_cost": CHAT_MESSAGE_CREDITS,
            "credit_cost_10s": None,
            "capabilities": dict(CHAT_MODEL_CAPABILITIES),
            "context_window": CHAT_MODEL_CONTEXT_WINDOW,
            "available": chat_available,
            "sort_order": 0,
        },
        {
            "id": IMAGE_MODEL_ID,
            "name": IMAGE_MODEL_NAME,
            "provider": MOCK_PROVIDER,
            "provider_model_id": IMAGE_MODEL_ID,
            "modality": "image",
            "description": IMAGE_MODEL_DESCRIPTION,
            "badge": None,
            "credit_cost": IMAGE_CREDITS,
            "credit_cost_10s": None,
            "capabilities": dict(IMAGE_MODEL_CAPABILITIES),
            "context_window": None,
            "available": mock_available,
            "sort_order": 10,
        },
        {
            "id": VIDEO_MODEL_ID,
            "name": VIDEO_MODEL_NAME,
            "provider": MOCK_PROVIDER,
            "provider_model_id": VIDEO_MODEL_ID,
            "modality": "video",
            "description": VIDEO_MODEL_DESCRIPTION,
            "badge": "Demo",
            "credit_cost": VIDEO_CREDITS,
            "credit_cost_10s": VIDEO_CREDITS_10S,
            "capabilities": dict(VIDEO_MODEL_CAPABILITIES),
            "context_window": None,
            "available": mock_available,
            "sort_order": 20,
        },
    ]


async def upsert_models(session: AsyncSession, settings: Settings) -> None:
    """Refresh the model catalog and drop rows that are no longer seeded.

    Deleting matters: if `AI_CHAT_MODEL` changes, the previous slug must not keep
    being advertised. Later phases attach `ON DELETE RESTRICT` foreign keys from
    Chats and Generations, so a referenced model makes this fail loudly instead of
    silently orphaning work.
    """
    values = model_values(settings)
    ids = [row["id"] for row in values]
    statement = insert(Model).values(values)
    statement = statement.on_conflict_do_update(
        index_elements=[Model.id],
        set_={
            "name": statement.excluded.name,
            "provider": statement.excluded.provider,
            "provider_model_id": statement.excluded.provider_model_id,
            "modality": statement.excluded.modality,
            "description": statement.excluded.description,
            "badge": statement.excluded.badge,
            "credit_cost": statement.excluded.credit_cost,
            "credit_cost_10s": statement.excluded.credit_cost_10s,
            "capabilities": statement.excluded.capabilities,
            "context_window": statement.excluded.context_window,
            "available": statement.excluded.available,
            "sort_order": statement.excluded.sort_order,
        },
    )
    await session.execute(statement)
    await session.execute(delete(Model).where(Model.id.notin_(ids)))
