"""Assets: binary outputs owned by a user.

An asset row names a file in storage. Rows are never served from the storage
directory directly; `GET /v1/assets/{id}` loads the row, checks permission, and
then hands the validated path to a file response. `storage_key` is unique and
relative, so the row is the only way to reach the bytes.

The owning foreign key cascades: deleting a user removes the rows. The file on
disk is left for the storage adapter to garbage collect, because a database
delete and a filesystem delete cannot share a transaction.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.ids import uuid7
from app.db.base import Base
from app.db.models.common import TimestampMixin

ASSET_KINDS = ("image", "video")

_KIND_CHECK = "kind IN ('" + "', '".join(ASSET_KINDS) + "')"


class Asset(TimestampMixin, Base):
    """One stored file. `generation_id` and `post_id` are filled in by later tickets."""

    __tablename__ = "assets"
    __table_args__ = (
        CheckConstraint(_KIND_CHECK, name="kind_valid"),
        CheckConstraint("size_bytes >= 0", name="size_bytes_non_negative"),
        CheckConstraint("width IS NULL OR width > 0", name="width_positive"),
        CheckConstraint("height IS NULL OR height > 0", name="height_positive"),
        CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="duration_ms_non_negative",
        ),
        CheckConstraint("btrim(mime_type) <> ''", name="mime_type_not_blank"),
        CheckConstraint(
            "btrim(storage_key) <> '' AND storage_key !~ '(^/|\\\\|(^|/)\\.\\.(/|$))'",
            name="storage_key_relative",
        ),
        UniqueConstraint("storage_key", name="uq_assets_storage_key"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid7)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    generation_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    post_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
