"""Asset persistence ports.

The domain talks to storage through this module so a later S3 adapter can replace
the local one without touching the delivery route.
"""

from __future__ import annotations

from app.storage.local import LocalStorage, StorageError, StoredFile, storage_key_for

__all__ = ["LocalStorage", "StorageError", "StoredFile", "storage_key_for"]
