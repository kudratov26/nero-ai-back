"""Asset use cases: create for an owner and resolve a permission-checked file.

Permission lives here rather than in the route. Today an asset is readable by its
owner only; B4.2 extends `can_read` with the published-post branch, and the route
already answers a denied read as `404 not_found` so an outsider cannot tell an
existing asset from a missing one.

The storage adapter is passed in rather than built from settings, so tests use a
temporary directory and the route owns the app-wide instance.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.problems import ApiError, ErrorCode
from app.core.ids import uuid7
from app.db.models import Asset
from app.db.models.asset import ASSET_KINDS
from app.db.session import session_scope
from app.storage.local import LocalStorage, StorageError, StoredFile, storage_key_for

ASSET_NOT_FOUND_DETAIL = "That asset does not exist."
INVALID_ASSET_KIND_DETAIL = "kind must be image or video."
INVALID_ASSET_DETAIL = "The asset content is not valid."

#: Owner bytes are private to the browser. B4.2 adds the public policy for a
#: published gallery asset alongside its permission rule.
OWNER_CACHE_CONTROL = "private, max-age=3600"

_MIME_EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "video/mp4": "mp4",
    "video/webm": "webm",
}


@dataclass(frozen=True)
class StoredAsset:
    """An asset row, the validated file it points at, and its cache policy."""

    asset: Asset
    file: StoredFile
    cache_control: str


def extension_for_mime(mime_type: str) -> str:
    """Pick the storage extension for a MIME type, defaulting to a safe key."""
    return _MIME_EXTENSIONS.get(mime_type.lower(), "bin")


def can_read(asset: Asset, *, user_id: UUID | None) -> bool:
    """Whether the caller may download the bytes.

    A signed-out caller never may. B4.2 adds the published-post case here, so
    this function stays the single place the rule is expressed.
    """
    return user_id is not None and asset.user_id == user_id


def cache_control_for(asset: Asset) -> str:
    """The cache policy for a readable asset.

    B4.2 returns the public policy for a published post's asset.
    """
    return OWNER_CACHE_CONTROL


async def create_asset(
    session_factory: async_sessionmaker[AsyncSession],
    storage: LocalStorage,
    *,
    user_id: UUID,
    kind: str,
    mime_type: str,
    data: bytes,
    width: int | None = None,
    height: int | None = None,
    duration_ms: int | None = None,
    generation_id: UUID | None = None,
    post_id: UUID | None = None,
) -> Asset:
    """Store bytes on disk and insert the row that names them.

    The id is chosen up front because the storage key embeds it. The row and the
    file cannot share a transaction, so the file is written first: a failed insert
    leaves an orphan the delivery path reports as `404`, never a row that points at
    missing bytes.
    """
    if kind not in ASSET_KINDS:
        raise ApiError(ErrorCode.INVALID_REQUEST, 400, detail=INVALID_ASSET_KIND_DETAIL)
    if not mime_type.strip() or not data:
        raise ApiError(ErrorCode.INVALID_REQUEST, 400, detail=INVALID_ASSET_DETAIL)

    asset_id = uuid7()
    key = storage_key_for(
        user_id=user_id,
        asset_id=asset_id,
        extension=extension_for_mime(mime_type),
    )
    stored = storage.write(key, data)

    async with session_scope(session_factory) as session:
        asset = Asset(
            id=asset_id,
            user_id=user_id,
            generation_id=generation_id,
            post_id=post_id,
            kind=kind,
            storage_key=key,
            mime_type=mime_type,
            size_bytes=stored.size_bytes,
            width=width,
            height=height,
            duration_ms=duration_ms,
        )
        session.add(asset)
        await session.flush()
        return asset


async def load_for_read(
    session_factory: async_sessionmaker[AsyncSession],
    storage: LocalStorage,
    *,
    asset_id: UUID,
    user_id: UUID | None,
) -> StoredAsset:
    """Return an asset the caller may read, or raise the documented 404.

    A missing row, another owner's row, a missing file, and a corrupt key are all
    `404`: each of them must look identical from outside.
    """
    async with session_scope(session_factory) as session:
        asset = await session.get(Asset, asset_id)
    if asset is None or not can_read(asset, user_id=user_id):
        raise _not_found()
    try:
        stored = storage.stat(asset.storage_key)
    except (OSError, StorageError):
        raise _not_found() from None
    return StoredAsset(
        asset=asset,
        file=stored,
        cache_control=cache_control_for(asset),
    )


def _not_found() -> ApiError:
    return ApiError(ErrorCode.NOT_FOUND, 404, detail=ASSET_NOT_FOUND_DETAIL)
