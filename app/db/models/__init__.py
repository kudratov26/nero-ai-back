"""SQLAlchemy models.

Importing this package registers every table on `Base.metadata`, which is what
Alembic autogenerate compares against.
"""

from __future__ import annotations

from app.db.models.asset import Asset
from app.db.models.chat import Chat
from app.db.models.credit import CreditAccount, CreditLedgerEntry
from app.db.models.generation import Generation
from app.db.models.idempotency import IdempotencyKey
from app.db.models.message import Message
from app.db.models.model import Model
from app.db.models.plan import Plan
from app.db.models.post import Post, PostLike
from app.db.models.session import UserSession
from app.db.models.user import User

__all__ = [
    "Asset",
    "Chat",
    "CreditAccount",
    "CreditLedgerEntry",
    "Generation",
    "IdempotencyKey",
    "Message",
    "Model",
    "Plan",
    "Post",
    "PostLike",
    "User",
    "UserSession",
]
