"""posts and post_likes

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-16 00:00:00.000000

Adds the curated Explore gallery and its likes. `posts.asset_id` is the canonical
cover link; the deferred `assets.post_id` foreign key is added here as the
back-reference for an attached asset.

The two tables reference each other (`posts.asset_id` -> `assets.id` and
`assets.post_id` -> `posts.id`). `assets` already exists, so the posts table is
created first and the back-reference is added afterwards rather than creating a
cycle inside `CREATE TABLE`.

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "posts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("author_id", sa.Uuid(), nullable=True),
        sa.Column("asset_id", sa.Uuid(), nullable=True),
        sa.Column("modality", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=False),
        sa.Column(
            "params",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "published",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "modality IN ('chat', 'image', 'video')",
            name=op.f("ck_posts_modality_valid"),
        ),
        sa.CheckConstraint(
            "btrim(title) <> ''",
            name=op.f("ck_posts_title_not_blank"),
        ),
        sa.CheckConstraint(
            "btrim(prompt) <> ''",
            name=op.f("ck_posts_prompt_not_blank"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(params) = 'object'",
            name=op.f("ck_posts_params_object"),
        ),
        sa.ForeignKeyConstraint(
            ["author_id"],
            ["users.id"],
            name=op.f("fk_posts_author_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.id"],
            name=op.f("fk_posts_asset_id_assets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["model_id"],
            ["models.id"],
            name=op.f("fk_posts_model_id_models"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_posts")),
    )
    op.create_index("ix_posts_author_id", "posts", ["author_id"], unique=False)
    op.create_index("ix_posts_asset_id", "posts", ["asset_id"], unique=False)
    op.create_index("ix_posts_model_id", "posts", ["model_id"], unique=False)
    op.create_index(
        "ix_posts_published_created_at_id",
        "posts",
        ["created_at", "id"],
        unique=False,
        postgresql_where=sa.text("published"),
    )

    op.create_table(
        "post_likes",
        sa.Column("post_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["post_id"],
            ["posts.id"],
            name=op.f("fk_post_likes_post_id_posts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_post_likes_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("post_id", "user_id", name=op.f("pk_post_likes")),
    )
    op.create_index("ix_post_likes_user_id", "post_likes", ["user_id"], unique=False)

    # `assets.post_id` existed before `posts` did, so a row written by B4.1 can
    # name a post that never existed. Clear those orphans before enforcing the
    # foreign key; a valid reference (only possible on a re-run after posts
    # exist) is kept.
    op.execute(
        "UPDATE assets SET post_id = NULL "
        "WHERE post_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM posts WHERE posts.id = assets.post_id)"
    )
    op.create_foreign_key(
        "fk_assets_post_id_posts",
        "assets",
        "posts",
        ["post_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_assets_post_id", "assets", ["post_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_assets_post_id", table_name="assets")
    op.drop_constraint("fk_assets_post_id_posts", "assets", type_="foreignkey")
    op.drop_index("ix_post_likes_user_id", table_name="post_likes")
    op.drop_table("post_likes")
    op.drop_index("ix_posts_published_created_at_id", table_name="posts")
    op.drop_index("ix_posts_model_id", table_name="posts")
    op.drop_index("ix_posts_asset_id", table_name="posts")
    op.drop_index("ix_posts_author_id", table_name="posts")
    op.drop_table("posts")
