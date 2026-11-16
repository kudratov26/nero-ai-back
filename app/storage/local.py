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
"""

from __future__ import annotations

import os
import stat as stat_module
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

STORAGE_PREFIX = "assets"


class StorageError(Exception):
    """A key that cannot be resolved inside the storage root."""


@dataclass(frozen=True)
class StoredFile:
    """What a delivery response needs to know about a file on disk."""

    path: Path
    size_bytes: int
    mtime: float


def storage_key_for(*, user_id: UUID, asset_id: UUID, extension: str) -> str:
    """Build the documented key. `extension` is without a leading dot."""
    return f"{STORAGE_PREFIX}/{user_id}/{asset_id}.{extension}"


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
        """Return the safe absolute path for a key, whether or not it exists."""
        return self._resolve(key)

    def stat(self, key: str) -> StoredFile:
        """Return size and mtime, or raise `FileNotFoundError`."""
        path = self._resolve(key)
        info = path.stat()
        if not stat_module.S_ISREG(info.st_mode):
            raise FileNotFoundError(key)
        return StoredFile(path=path, size_bytes=info.st_size, mtime=info.st_mtime)

    def write(self, key: str, data: bytes) -> StoredFile:
        """Write bytes atomically: a partial write never becomes visible.

        The file is written to a sibling temp name and renamed into place, so a
        reader sees either the old content or the complete new content.
        """
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temp.write_bytes(data)
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
        return self.stat(key)

    def delete(self, key: str) -> None:
        """Remove a file, ignoring a key that is already gone."""
        try:
            path = self._resolve(key)
        except StorageError:
            return
        path.unlink(missing_ok=True)
