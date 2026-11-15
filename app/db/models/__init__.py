"""SQLAlchemy models.

Importing this package registers every table on `Base.metadata`, which is what
Alembic autogenerate compares against.
"""

from __future__ import annotations

from app.db.models.credit import CreditAccount, CreditLedgerEntry
from app.db.models.model import Model
from app.db.models.plan import Plan
from app.db.models.session import UserSession
from app.db.models.user import User

__all__ = [
    "CreditAccount",
    "CreditLedgerEntry",
    "Model",
    "Plan",
    "User",
    "UserSession",
]
