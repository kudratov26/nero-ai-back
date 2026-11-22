"""Build the configured storage adapter.

The factory is the only place settings turn into an adapter, so the API, worker,
seed, and tests all agree on which backend is active.
"""

from __future__ import annotations

from app.core.config import Settings
from app.storage.base import Storage
from app.storage.local import LocalStorage
from app.storage.s3 import S3Storage


def build_storage(settings: Settings) -> Storage:
    """Return the storage adapter selected by `STORAGE_BACKEND`."""
    if settings.storage_backend == "s3":
        if (
            settings.s3_bucket is None
            or settings.s3_access_key_id is None
            or settings.s3_secret_access_key is None
        ):
            raise RuntimeError("S3 storage is selected but credentials are incomplete")
        return S3Storage(
            bucket=settings.s3_bucket,
            region=settings.s3_region,
            endpoint_url=(
                settings.s3_endpoint_url
                if settings.s3_endpoint_url is not None
                else f"https://s3.{settings.s3_region}.amazonaws.com"
            ),
            access_key=settings.s3_access_key_id.get_secret_value(),
            secret_key=settings.s3_secret_access_key.get_secret_value(),
            session_token=(
                settings.s3_session_token.get_secret_value()
                if settings.s3_session_token is not None
                else None
            ),
            prefix=settings.s3_prefix,
        )
    return LocalStorage(settings.storage_dir)
