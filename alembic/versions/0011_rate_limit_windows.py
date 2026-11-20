"""rate limit windows

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-20 00:00:00.000000

Stores fixed-window rate-limit counters in PostgreSQL so an attempt budget
survives an API restart and is shared by every API process. One row per
throttle key; `window_end` is indexed for the startup prune.

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rate_limit_windows",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("window_end", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.CheckConstraint("btrim(key) <> ''", name=op.f("ck_rate_limit_windows_key_not_blank")),
        sa.CheckConstraint("count >= 0", name=op.f("ck_rate_limit_windows_count_non_negative")),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_rate_limit_windows")),
    )
    op.create_index(
        "ix_rate_limit_windows_window_end",
        "rate_limit_windows",
        ["window_end"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_rate_limit_windows_window_end", table_name="rate_limit_windows")
    op.drop_table("rate_limit_windows")
