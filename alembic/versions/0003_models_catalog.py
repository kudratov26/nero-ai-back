"""models catalog

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-15 11:29:47.900580

The catalog rows themselves are not inserted here: `available` depends on
configuration, so the seed step (`python -m app.cli seed`) writes them.

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "models",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("provider_model_id", sa.Text(), nullable=False),
        sa.Column("modality", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("badge", sa.Text(), nullable=True),
        sa.Column("credit_cost", sa.Integer(), nullable=False),
        sa.Column("credit_cost_10s", sa.Integer(), nullable=True),
        sa.Column(
            "capabilities",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("context_window", sa.Integer(), nullable=True),
        sa.Column("available", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.CheckConstraint("btrim(id) <> ''", name=op.f("ck_models_id_not_blank")),
        sa.CheckConstraint("btrim(name) <> ''", name=op.f("ck_models_name_not_blank")),
        sa.CheckConstraint(
            "credit_cost_10s IS NULL OR modality = 'video'",
            name=op.f("ck_models_credit_cost_10s_video_only"),
        ),
        sa.CheckConstraint(
            "modality IN ('chat', 'image', 'video')", name=op.f("ck_models_modality_valid")
        ),
        sa.CheckConstraint(
            "context_window IS NULL OR context_window > 0",
            name=op.f("ck_models_context_window_positive"),
        ),
        sa.CheckConstraint("credit_cost >= 0", name=op.f("ck_models_credit_cost_non_negative")),
        sa.CheckConstraint(
            "credit_cost_10s IS NULL OR credit_cost_10s >= 0",
            name=op.f("ck_models_credit_cost_10s_non_negative"),
        ),
        sa.CheckConstraint("sort_order >= 0", name=op.f("ck_models_sort_order_non_negative")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_models")),
    )


def downgrade() -> None:
    op.drop_table("models")
