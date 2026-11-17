"""generations

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-17 00:00:00.000000

Adds the Generation queue and attaches the deferred `assets.generation_id`
foreign key. Existing assets could carry any uuid in that column before this
migration, so orphans are cleared before the constraint is created.

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "generations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("modality", sa.Text(), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column(
            "params",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Text(),
            server_default=sa.text("'queued'"),
            nullable=False,
        ),
        sa.Column(
            "progress",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("cost", sa.Integer(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("provider_job_id", sa.Text(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column(
            "attempts",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "max_attempts",
            sa.Integer(),
            server_default=sa.text("3"),
            nullable=False,
        ),
        sa.Column("locked_by", sa.Text(), nullable=True),
        sa.Column("locked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "modality IN ('image', 'video')",
            name=op.f("ck_generations_modality_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')",
            name=op.f("ck_generations_status_valid"),
        ),
        sa.CheckConstraint(
            "char_length(prompt) BETWEEN 1 AND 4000",
            name=op.f("ck_generations_prompt_length"),
        ),
        sa.CheckConstraint(
            "btrim(prompt) <> ''",
            name=op.f("ck_generations_prompt_not_blank"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(params) = 'object'",
            name=op.f("ck_generations_params_object"),
        ),
        sa.CheckConstraint(
            "progress BETWEEN 0 AND 100",
            name=op.f("ck_generations_progress_range"),
        ),
        sa.CheckConstraint(
            "cost >= 0",
            name=op.f("ck_generations_cost_non_negative"),
        ),
        sa.CheckConstraint(
            "attempts >= 0",
            name=op.f("ck_generations_attempts_non_negative"),
        ),
        sa.CheckConstraint(
            "max_attempts >= 1",
            name=op.f("ck_generations_max_attempts_positive"),
        ),
        sa.CheckConstraint(
            "btrim(provider) <> ''",
            name=op.f("ck_generations_provider_not_blank"),
        ),
        sa.CheckConstraint(
            "error_code IS NULL OR btrim(error_code) <> ''",
            name=op.f("ck_generations_error_code_not_blank"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_generations_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["model_id"],
            ["models.id"],
            name=op.f("fk_generations_model_id_models"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_generations")),
    )
    op.create_index(
        "ix_generations_user_id_created_at_id",
        "generations",
        ["user_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_generations_status_created_at",
        "generations",
        ["status", "created_at"],
        unique=False,
    )
    op.create_index("ix_generations_model_id", "generations", ["model_id"], unique=False)

    # Before 0009 `assets.generation_id` was an unconstrained uuid, so a row may
    # name a Generation that never existed. Clear those orphans, as 0008 does
    # for `assets.post_id`, before enforcing the foreign key.
    op.execute(
        "UPDATE assets SET generation_id = NULL "
        "WHERE generation_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM generations WHERE generations.id = assets.generation_id)"
    )
    op.create_foreign_key(
        "fk_assets_generation_id_generations",
        "assets",
        "generations",
        ["generation_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_assets_generation_id", "assets", ["generation_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_assets_generation_id", table_name="assets")
    op.drop_constraint("fk_assets_generation_id_generations", "assets", type_="foreignkey")
    op.drop_index("ix_generations_model_id", table_name="generations")
    op.drop_index("ix_generations_status_created_at", table_name="generations")
    op.drop_index("ix_generations_user_id_created_at_id", table_name="generations")
    op.drop_table("generations")
