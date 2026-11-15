"""Idempotent create operations.

A create endpoint requires an `Idempotency-Key` header. This service records the
outcome of the first request under `(user_id, key)` and replays it for retries,
so a dropped response or a double click cannot create two resources or spend
Credits twice.

The record is written in the same transaction as the resource, so a request that
fails leaves no key behind. A concurrent duplicate loses the insert race, rolls
its work back, and returns the winner's resource.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.problems import ApiError, ErrorCode
from app.db.models import IdempotencyKey
from app.db.session import session_scope

#: Canonical `operation` values, so a key cannot be reused across operations.
GENERATION_CREATE = "generation.create"
CHAT_MESSAGE = "chat.message"

CONFLICT_DETAIL = "This Idempotency-Key was already used with a different request."

#: The primary key of `idempotency_keys`. A violation of it means two requests
#: raced on the same `(user_id, key)` and this one lost.
_RACE_CONSTRAINT = "pk_idempotency_keys"

#: One retry is enough: by the time the loser re-reads, the winner has committed.
_MAX_ATTEMPTS = 2


@dataclass(frozen=True)
class CreateOutcome:
    """The resource a create call resolved to, and whether it was replayed."""

    resource_id: UUID
    replayed: bool


def hash_request(payload: Mapping[str, Any]) -> str:
    """Return a stable SHA-256 hex digest of a request body.

    Keys are sorted and whitespace stripped, so two bodies that are equal as
    JSON hash equally regardless of key order or formatting.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


async def find(session: AsyncSession, *, user_id: UUID, key: str) -> IdempotencyKey | None:
    """Return the stored key for `(user_id, key)`, or `None`."""
    return await session.scalar(
        select(IdempotencyKey).where(
            IdempotencyKey.user_id == user_id,
            IdempotencyKey.key == key,
        )
    )


def _ensure_same_request(stored: IdempotencyKey, *, operation: str, request_hash: str) -> None:
    if stored.operation != operation or stored.request_hash != request_hash:
        raise ApiError(ErrorCode.IDEMPOTENCY_CONFLICT, 409, detail=CONFLICT_DETAIL)


def _lost_the_race(exc: IntegrityError) -> bool:
    return _RACE_CONSTRAINT in str(exc.orig)


async def execute(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: UUID,
    key: str,
    operation: str,
    request_hash: str,
    create: Callable[[AsyncSession], Awaitable[UUID]],
) -> CreateOutcome:
    """Create a resource once for `(user_id, key)`.

    `create` runs in the same transaction as the idempotency record and returns
    the new resource's id. On a replay it is not called and the original id is
    returned. If a concurrent call with the same key wins the race, this call
    discards its work and replays the winner's resource instead.
    """
    last_error: IntegrityError | None = None
    for _ in range(_MAX_ATTEMPTS):
        try:
            async with session_scope(session_factory) as session:
                existing = await find(session, user_id=user_id, key=key)
                if existing is not None:
                    _ensure_same_request(existing, operation=operation, request_hash=request_hash)
                    return CreateOutcome(resource_id=existing.resource_id, replayed=True)
                resource_id = await create(session)
                session.add(
                    IdempotencyKey(
                        user_id=user_id,
                        key=key,
                        operation=operation,
                        request_hash=request_hash,
                        resource_id=resource_id,
                    )
                )
                await session.flush()
                return CreateOutcome(resource_id=resource_id, replayed=False)
        except IntegrityError as exc:
            if not _lost_the_race(exc):
                raise
            last_error = exc
    # Reached only if the retry also lost the race, which a committed winner
    # makes impossible; surface the original error rather than spin.
    assert last_error is not None
    raise last_error
