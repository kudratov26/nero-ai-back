"""Asset persistence ports.

The domain talks to storage through this module so the local and S3 adapters are
interchangeable. `build_storage` is the only settings-aware constructor.
"""

from __future__ import annotations

from app.storage.base import Storage, StorageError, StoredFile, storage_key_for
from app.storage.factory import build_storage
from app.storage.local import LocalStorage
from app.storage.s3 import S3Storage

__all__ = [
    "LocalStorage",
    "S3Storage",
    "Storage",
    "StorageError",
    "StoredFile",
    "build_storage",
    "storage_key_for",
]
