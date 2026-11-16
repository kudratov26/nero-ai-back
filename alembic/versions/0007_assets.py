"""assets

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-16 00:00:00.000000

One row per binary asset stored on disk. `generation_id` and `post_id` foreign
keys are added in later migrations as those tables are introduced.

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "assets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("generation_id", sa.Uuid(), nullable=True),
        sa.Column("post_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('image', 'video')",
            name=op.f("ck_assets_kind_valid"),
        ),
        sa.CheckConstraint(
            "size_bytes >= 0",
            name=op.f("ck_assets_size_bytes_non_negative"),
        ),
        sa.CheckConstraint(
            "width IS NULL OR width > 0",
            name=op.f("ck_assets_width_positive"),
        ),
        sa.CheckConstraint(
            "height IS NULL OR height > 0",
            name=op.f("ck_assets_height_positive"),
        ),
        sa.CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name=op.f("ck_assets_duration_ms_non_negative"),
        ),
        sa.CheckConstraint(
            "btrim(mime_type) <> ''",
            name=op.f("ck_assets_mime_type_not_blank"),
        ),
        sa.CheckConstraint(
            "btrim(storage_key) <> '' AND storage_key !~ '(^/|\\\\|(^|/)\\.\\.(/|$))'",
            name=op.f("ck_assets_storage_key_relative"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_assets_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_assets")),
        sa.UniqueConstraint("storage_key", name=op.f("uq_assets_storage_key")),
    )
    op.create_index(
        "ix_assets_user_id",
        "assets",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_assets_user_id", table_name="assets")
    op.drop_table("assets")
