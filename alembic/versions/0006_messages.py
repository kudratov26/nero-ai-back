"""messages

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-15 15:30:00.000000

One row per chat turn. The assistant placeholder is written before the provider
call and updated in place, so a failed stream still leaves the partial reply.
The partial unique index on `(chat_id, client_id)` is the database backstop for
a client retrying the same user message.

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("chat_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("client_id", sa.Text(), nullable=True),
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
        sa.CheckConstraint(
            "role IN ('system', 'user', 'assistant')",
            name=op.f("ck_messages_role_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('complete', 'streaming', 'failed', 'cancelled')",
            name=op.f("ck_messages_status_valid"),
        ),
        sa.CheckConstraint(
            "prompt_tokens IS NULL OR prompt_tokens >= 0",
            name=op.f("ck_messages_prompt_tokens_non_negative"),
        ),
        sa.CheckConstraint(
            "completion_tokens IS NULL OR completion_tokens >= 0",
            name=op.f("ck_messages_completion_tokens_non_negative"),
        ),
        sa.CheckConstraint(
            "client_id IS NULL OR btrim(client_id) <> ''",
            name=op.f("ck_messages_client_id_not_blank"),
        ),
        sa.ForeignKeyConstraint(
            ["chat_id"],
            ["chats.id"],
            name=op.f("fk_messages_chat_id_chats"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_messages_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["model_id"],
            ["models.id"],
            name=op.f("fk_messages_model_id_models"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
    )
    op.create_index(
        "ix_messages_chat_id_created_at",
        "messages",
        ["chat_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "uq_messages_chat_id_client_id",
        "messages",
        ["chat_id", "client_id"],
        unique=True,
        postgresql_where=sa.text("client_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_messages_chat_id_client_id", table_name="messages")
    op.drop_index("ix_messages_chat_id_created_at", table_name="messages")
    op.drop_table("messages")
