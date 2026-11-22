"""Storage port shared by the local and S3 adapters.

Asset bytes live outside PostgreSQL. The port is deliberately small: write one
object, stat it, stream a byte range, and delete it. Keys are always relative
paths of the form `assets/{userId}/{assetId}.{ext}`; the adapter decides where
that path physically lives.

`iter_range` returns an async iterator so a large object never has to be held in
memory. `end` is exclusive; `None` means "through the last byte".
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

STORAGE_PREFIX = "assets"


class StorageError(Exception):
    """A key that cannot be resolved, or a storage backend that failed."""


@dataclass(frozen=True)
class StoredFile:
    """What a delivery response needs to know about one stored object."""

    size_bytes: int
    mtime: float
    etag: str | None = None


def storage_key_for(*, user_id: UUID, asset_id: UUID, extension: str) -> str:
    """Build the documented key. `extension` is without a leading dot."""
    return f"{STORAGE_PREFIX}/{user_id}/{asset_id}.{extension}"


class Storage(Protocol):
    """The async port implemented by `LocalStorage` and `S3Storage`."""

    async def write(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
    ) -> StoredFile:
        """Store `data`, replacing any existing object at `key`."""
        ...

    async def stat(self, key: str) -> StoredFile:
        """Return object metadata, or raise `FileNotFoundError`."""
        ...

    def iter_range(
        self, key: str, *, start: int = 0, end: int | None = None
    ) -> AsyncIterator[bytes]:
        """Yield bytes from `start` (inclusive) to `end` (exclusive)."""
        ...

    async def delete(self, key: str) -> None:
        """Remove an object, ignoring a key that is already gone."""
        ...

    async def aclose(self) -> None:
        """Release backend resources. Safe to call more than once."""
        ...
