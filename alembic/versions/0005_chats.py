"""chats

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-15 13:10:00.000000

One row per conversation. `deleted_at` makes deletion soft, and the partial index
on `(user_id, updated_at DESC)` serves the own-chats listing without indexing
rows the caller has already hidden.

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chats",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column(
            "title_is_custom",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("model_id", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "char_length(title) BETWEEN 1 AND 80", name=op.f("ck_chats_title_length")
        ),
        sa.CheckConstraint("btrim(title) <> ''", name=op.f("ck_chats_title_not_blank")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_chats_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["model_id"],
            ["models.id"],
            name=op.f("fk_chats_model_id_models"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chats")),
    )
    op.create_index(
        "ix_chats_user_id_updated_at",
        "chats",
        ["user_id", "updated_at", "id"],
        unique=False,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_chats_user_id_updated_at", table_name="chats")
    op.drop_table("chats")
