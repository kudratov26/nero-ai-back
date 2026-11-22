"""Local asset storage.

Storage keys are relative paths of the form `assets/{userId}/{assetId}.{ext}`.
Two rules keep a corrupted key from reading outside the root:

1. The key is resolved against the root and the result must stay under it, after
   following symlinks (`Path.resolve()`), so `..`, a symlink, or an absolute key
   cannot escape.
2. The resolved path must be a regular file, so a key that lands on a device or
   directory fails instead of being opened.

Nothing else in the app builds a filesystem path from a client value. The route
loads an `Asset` row first, and the row is the only source of a key.

The adapter exposes the same async port as S3, but the work is small local file
I/O, so it runs in worker threads rather than blocking the event loop.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat as stat_module
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

from app.storage.base import StorageError, StoredFile

CHUNK_SIZE = 64 * 1024


def _etag(info: os.stat_result) -> str:
    """A strong validator that changes when the bytes could have changed."""
    digest = hashlib.sha256(f"{info.st_mtime_ns}-{info.st_size}".encode()).hexdigest()
    return f'"{digest[:32]}"'


class LocalStorage:
    """A storage root on the local filesystem."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root).expanduser()

    def _resolve(self, key: str) -> Path:
        """Resolve `key` under the root, rejecting anything that escapes it."""
        if not key or key.startswith(("/", "\\")):
            raise StorageError("storage key must be relative")
        if "\x00" in key:
            raise StorageError("storage key must not contain a null byte")
        if "\\" in key:
            raise StorageError("storage key must use forward slashes")
        if ".." in Path(key).parts:
            raise StorageError("storage key must not contain '..'")

        root = self._root.resolve()
        candidate = (root / key).resolve()
        # `is_relative_to` after `resolve` compares real paths, so a symlink
        # pointing outside the root is rejected even though the key looks clean.
        if candidate == root or not candidate.is_relative_to(root):
            raise StorageError("storage key escapes the storage root")
        return candidate

    def path_for(self, key: str) -> Path:
        """Return the safe absolute path for a key, whether or not it exists.

        Only the local adapter has a path. Application code uses `stat` and
        `iter_range`, which every adapter implements.
        """
        return self._resolve(key)

    def _stat_path(self, path: Path) -> StoredFile:
        info = path.stat()
        if not stat_module.S_ISREG(info.st_mode):
            raise FileNotFoundError(path.name)
        return StoredFile(size_bytes=info.st_size, mtime=info.st_mtime, etag=_etag(info))

    async def stat(self, key: str) -> StoredFile:
        """Return size, mtime, and ETag, or raise `FileNotFoundError`."""
        return await asyncio.to_thread(self._stat_path, self._resolve(key))

    def _write_sync(self, key: str, data: bytes) -> StoredFile:
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temp.write_bytes(data)
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
        return self._stat_path(path)

    async def write(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
    ) -> StoredFile:
        """Write bytes atomically: a partial write never becomes visible.

        The file is written to a sibling temp name and renamed into place, so a
        reader sees either the old content or the complete new content. The
        content type is unused locally; S3 stores it as object metadata.
        """
        del content_type
        return await asyncio.to_thread(self._write_sync, key, data)

    async def iter_range(
        self,
        key: str,
        *,
        start: int = 0,
        end: int | None = None,
    ) -> AsyncIterator[bytes]:
        """Yield one open-ended or bounded byte range from a local file."""
        path = self._resolve(key)
        info = await asyncio.to_thread(path.stat)
        if not stat_module.S_ISREG(info.st_mode):
            raise FileNotFoundError(key)
        stop = info.st_size if end is None else min(end, info.st_size)
        if start < 0 or start > info.st_size:
            raise StorageError("storage range starts outside the object")

        file = await asyncio.to_thread(path.open, "rb")
        try:
            await asyncio.to_thread(file.seek, start)
            remaining = stop - start
            while remaining > 0:
                chunk = await asyncio.to_thread(file.read, min(CHUNK_SIZE, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk
        finally:
            await asyncio.to_thread(file.close)

    async def delete(self, key: str) -> None:
        """Remove a file, ignoring a key that is already gone."""
        try:
            path = self._resolve(key)
        except StorageError:
            return
        await asyncio.to_thread(path.unlink, True)

    async def aclose(self) -> None:
        """The local adapter holds no resources."""
