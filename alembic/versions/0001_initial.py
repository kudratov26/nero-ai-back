"""initial

Revision ID: 0001
Revises:
Create Date: 2026-11-14

Empty baseline. Tables arrive with their phase (users and sessions in B1.1).
"""

from collections.abc import Sequence

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
