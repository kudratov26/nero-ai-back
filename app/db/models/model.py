"""The server-authoritative model catalog.

Clients never send a price; they send a model `id` and the server reads the cost
from this table. Rows are written only by the seed step (`python -m app.cli seed`),
which computes `available` from configuration so an unconfigured provider is never
advertised.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Boolean, CheckConstraint, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

MODALITIES = ("chat", "image", "video")

_MODALITY_CHECK = "modality IN ('" + "', '".join(MODALITIES) + "')"


class Model(Base):
    """A selectable chat, image, or video model."""

    __tablename__ = "models"
    __table_args__ = (
        CheckConstraint(_MODALITY_CHECK, name="modality_valid"),
        CheckConstraint("btrim(id) <> ''", name="id_not_blank"),
        CheckConstraint("btrim(name) <> ''", name="name_not_blank"),
        CheckConstraint("credit_cost >= 0", name="credit_cost_non_negative"),
        CheckConstraint(
            "credit_cost_10s IS NULL OR credit_cost_10s >= 0",
            name="credit_cost_10s_non_negative",
        ),
        # `credit_cost` covers 5 seconds of video, so a 10-second price only makes
        # sense for video models.
        CheckConstraint(
            "credit_cost_10s IS NULL OR modality = 'video'",
            name="credit_cost_10s_video_only",
        ),
        CheckConstraint(
            "context_window IS NULL OR context_window > 0",
            name="context_window_positive",
        ),
        CheckConstraint("sort_order >= 0", name="sort_order_non_negative"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    provider_model_id: Mapped[str] = mapped_column(Text, nullable=False)
    modality: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    badge: Mapped[str | None] = mapped_column(Text, nullable=True)
    credit_cost: Mapped[int] = mapped_column(Integer, nullable=False)
    credit_cost_10s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    capabilities: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    context_window: Mapped[int | None] = mapped_column(Integer, nullable=True)
    available: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)
