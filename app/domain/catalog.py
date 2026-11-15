"""Catalog reads.

The catalog is server-authoritative: prices and availability come from the `models`
and `plans` tables, which only the seed step writes. Deliberately no caching — the
tables are tiny and a stale price is worse than a query.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.problems import ApiError, ErrorCode
from app.db.models import Model, Plan
from app.db.session import session_scope

MODEL_NOT_FOUND_DETAIL = "That model does not exist."


async def list_plans(
    session_factory: async_sessionmaker[AsyncSession],
) -> list[Plan]:
    """Return every plan, in display order."""
    async with session_scope(session_factory) as session:
        rows = await session.scalars(select(Plan).order_by(Plan.sort_order, Plan.id))
        return list(rows)


async def list_models(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    modality: str | None = None,
) -> list[Model]:
    """Return available models, optionally narrowed to one modality.

    Models whose provider is not configured are never listed, so the catalog can
    not advertise work the server cannot do.
    """
    statement = select(Model).where(Model.available.is_(True)).order_by(Model.sort_order, Model.id)
    if modality is not None:
        statement = statement.where(Model.modality == modality)
    async with session_scope(session_factory) as session:
        rows = await session.scalars(statement)
        return list(rows)


async def get_model(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    model_id: str,
) -> Model:
    """Return one available model, or `404 not_found`.

    An unavailable model is indistinguishable from an unknown one on purpose: it
    must not be advertised.
    """
    async with session_scope(session_factory) as session:
        row = await session.scalar(
            select(Model).where(Model.id == model_id, Model.available.is_(True))
        )
        if row is None:
            raise ApiError(ErrorCode.NOT_FOUND, 404, detail=MODEL_NOT_FOUND_DETAIL)
        return row
