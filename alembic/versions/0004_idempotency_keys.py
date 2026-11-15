"""idempotency keys

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-15 12:30:00.000000

Records the outcome of a create request under `(user_id, key)`, so a retried
request replays the original resource instead of creating a second one.

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "idempotency_keys",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("btrim(key) <> ''", name=op.f("ck_idempotency_keys_key_not_blank")),
        sa.CheckConstraint("char_length(key) <= 255", name=op.f("ck_idempotency_keys_key_length")),
        sa.CheckConstraint(
            "btrim(operation) <> ''", name=op.f("ck_idempotency_keys_operation_not_blank")
        ),
        sa.CheckConstraint(
            "char_length(request_hash) = 64",
            name=op.f("ck_idempotency_keys_request_hash_sha256"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_idempotency_keys_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "key", name=op.f("pk_idempotency_keys")),
    )
    op.create_index(
        "ix_idempotency_keys_created_at", "idempotency_keys", ["created_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_idempotency_keys_created_at", table_name="idempotency_keys")
    op.drop_table("idempotency_keys")
