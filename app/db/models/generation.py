"""Generations: one Image or Video job owned by one account.

A Generation is created by `POST /v1/generations`, which validates the request
against the selected Model's advertised capabilities, computes the cost on the
server, and reserves that many Credits before the row is committed. The worker
(Phase 5.2) later claims queued rows and drives them to `succeeded` or `failed`.

The row outlives the request. `status` therefore carries the lifecycle, `cost`
carries the reservation amount, and `locked_by`/`locked_at` are the worker's
lease. `provider_job_id` lets a provider job be polled or cancelled after a
restart.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    TIMESTAMP,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Text,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.ids import uuid7
from app.db.base import Base
from app.db.models.common import TimestampMixin

GENERATION_MODALITIES = ("image", "video")
GENERATION_STATUSES = ("queued", "running", "succeeded", "failed", "cancelled")

MAX_PROMPT_LENGTH = 4000
DEFAULT_MAX_ATTEMPTS = 3

_MODALITY_CHECK = "modality IN ('" + "', '".join(GENERATION_MODALITIES) + "')"
_STATUS_CHECK = "status IN ('" + "', '".join(GENERATION_STATUSES) + "')"


class Generation(TimestampMixin, Base):
    """One Image or Video job. Every read is scoped by `user_id`."""

    __tablename__ = "generations"
    __table_args__ = (
        CheckConstraint(_MODALITY_CHECK, name="modality_valid"),
        CheckConstraint(_STATUS_CHECK, name="status_valid"),
        CheckConstraint(
            f"char_length(prompt) BETWEEN 1 AND {MAX_PROMPT_LENGTH}",
            name="prompt_length",
        ),
        CheckConstraint("btrim(prompt) <> ''", name="prompt_not_blank"),
        CheckConstraint("jsonb_typeof(params) = 'object'", name="params_object"),
        CheckConstraint("progress BETWEEN 0 AND 100", name="progress_range"),
        CheckConstraint("cost >= 0", name="cost_non_negative"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        CheckConstraint("max_attempts >= 1", name="max_attempts_positive"),
        CheckConstraint("btrim(provider) <> ''", name="provider_not_blank"),
        CheckConstraint(
            "error_code IS NULL OR btrim(error_code) <> ''",
            name="error_code_not_blank",
        ),
        # The owner's newest-first page and its keyset cursor.
        Index(
            "ix_generations_user_id_created_at_id",
            "user_id",
            "created_at",
            "id",
        ),
        # The worker's claim query in B5.2.
        Index("ix_generations_status_created_at", "status", "created_at"),
        Index("ix_generations_model_id", "model_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid7)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    modality: Mapped[str] = mapped_column(Text, nullable=False)
    model_id: Mapped[str] = mapped_column(
        Text,
        # A catalog row cannot be deleted while a Generation names it.
        ForeignKey("models.id", ondelete="RESTRICT"),
        nullable=False,
    )
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    params: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="queued",
        server_default=text("'queued'"),
    )
    progress: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    #: The number of Credits reserved when the row was created.
    cost: Mapped[int] = mapped_column(Integer, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    provider_job_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=DEFAULT_MAX_ATTEMPTS,
        server_default=text(str(DEFAULT_MAX_ATTEMPTS)),
    )
    locked_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=True,
    )
    started_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=True,
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=True,
    )
