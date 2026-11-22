"""Reset a caller's demo workspace.

A demo reset clears only the caller's work — Chats (and their Messages),
Generations, Assets, likes, and create-idempotency records — then restores the
account to the current plan allowance with one `reset` ledger entry. Rows and
Credits commit together, so a reset either empties the workspace and restores the
allowance, or changes nothing.

Files are removed after the commit. The Asset row is the only way to reach the
bytes, so a file whose row is gone is already unreachable; deleting it is
cleanup, not part of the transaction's correctness. A failed unlink leaves an
orphan file, never a live row.

Every statement is scoped by `user_id`. Published Explore posts are owned by the
system author, so a caller's reset cannot touch the gallery; only the caller's
likes are removed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

import structlog
from sqlalchemy import delete, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Asset, Chat, Generation, IdempotencyKey, PostLike
from app.db.session import session_scope
from app.domain import credits
from app.storage.base import Storage, StorageError

log = structlog.get_logger(__name__)

DEMO_RESET_REASON = "Demo reset"
DEMO_RESET_REF = "demo_reset"


@dataclass(frozen=True)
class DemoResetResult:
    """Rows one reset removed, for the audit log and tests."""

    chats: int
    generations: int
    assets: int
    likes: int
    idempotency_keys: int


def _removed(result: Any) -> int:
    return cast(CursorResult[Any], result).rowcount or 0


async def reset_workspace(
    session_factory: async_sessionmaker[AsyncSession],
    storage: Storage,
    *,
    user_id: UUID,
) -> DemoResetResult:
    """Delete the caller's resources and restore the plan allowance once."""
    async with session_scope(session_factory) as session:
        likes = _removed(await session.execute(delete(PostLike).where(PostLike.user_id == user_id)))
        chats = _removed(await session.execute(delete(Chat).where(Chat.user_id == user_id)))
        # Generations go before Assets so a worker settling concurrently cannot
        # insert an output that the Asset sweep has already passed.
        generations = _removed(
            await session.execute(delete(Generation).where(Generation.user_id == user_id))
        )
        storage_keys = list(
            await session.scalars(select(Asset.storage_key).where(Asset.user_id == user_id))
        )
        assets = _removed(await session.execute(delete(Asset).where(Asset.user_id == user_id)))
        idempotency_keys = _removed(
            await session.execute(delete(IdempotencyKey).where(IdempotencyKey.user_id == user_id))
        )
        await credits.reset(
            session,
            user_id=user_id,
            reason=DEMO_RESET_REASON,
            ref_type=DEMO_RESET_REF,
            ref_id=None,
        )

    failed_unlinks = 0
    for key in storage_keys:
        try:
            await storage.delete(key)
        except (OSError, StorageError):
            failed_unlinks += 1

    result = DemoResetResult(
        chats=chats,
        generations=generations,
        assets=assets,
        likes=likes,
        idempotency_keys=idempotency_keys,
    )
    log.info(
        "demo.reset",
        user_id=str(user_id),
        chats=result.chats,
        generations=result.generations,
        assets=result.assets,
        likes=result.likes,
        idempotency_keys=result.idempotency_keys,
        failed_unlinks=failed_unlinks,
    )
    return result
