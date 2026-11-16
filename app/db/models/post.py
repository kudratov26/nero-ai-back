"""Explore posts and their likes.

A Post is a curated gallery entry: a prompt, the Model that produced it, the
params needed to remix it, and the Asset that renders it. Posts are read-only in
v1; users cannot publish a Generation, so rows are written only by the seed step.

`posts.asset_id` is the canonical cover asset. `assets.post_id` is a back-reference
for an asset attached to a post; asset delivery treats either link as membership
when the post is published, so an attachment that is not the cover stays readable.

A Like is a unique `(post_id, user_id)` row. Both like and unlike are idempotent:
the primary key makes a repeat insert a no-op, and a repeat delete removes zero
rows. Counts are derived from these rows rather than stored on the post, so there
is no denormalized counter to drift.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Text,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.ids import uuid7
from app.db.base import Base
from app.db.models.common import TimestampMixin
from app.db.models.model import MODALITIES

_MODALITY_CHECK = "modality IN ('" + "', '".join(MODALITIES) + "')"


class Post(TimestampMixin, Base):
    """One curated Explore gallery item."""

    __tablename__ = "posts"
    __table_args__ = (
        CheckConstraint(_MODALITY_CHECK, name="modality_valid"),
        CheckConstraint("btrim(title) <> ''", name="title_not_blank"),
        CheckConstraint("btrim(prompt) <> ''", name="prompt_not_blank"),
        CheckConstraint("jsonb_typeof(params) = 'object'", name="params_object"),
        # Only published rows appear in Explore, and every listing is newest
        # first, so the partial index matches the read path exactly.
        Index(
            "ix_posts_published_created_at_id",
            "created_at",
            "id",
            postgresql_where=text("published"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid7)
    # Seeds use a system author. `SET NULL` keeps a post when its author is
    # removed, which is preferable to losing gallery content.
    author_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # The cover asset. `RESTRICT` keeps a published post from losing its media.
    asset_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("assets.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    modality: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    model_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("models.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    params: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    published: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )


class PostLike(TimestampMixin, Base):
    """A unique account-to-post like."""

    __tablename__ = "post_likes"
    __table_args__ = (
        # The primary key already indexes `(post_id, user_id)` and serves the
        # count for one post. This index serves "what did this account like?".
        Index("ix_post_likes_user_id", "user_id"),
    )

    post_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("posts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
