"""holds

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-18 00:00:00.000000

Adds the index the expired-hold sweeper uses: assistant messages left
`streaming` past the hold TTL are found by `status` and `updated_at` so the
sweep does not scan the whole messages table.

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_messages_status_updated_at",
        "messages",
        ["status", "updated_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_messages_status_updated_at", table_name="messages")
