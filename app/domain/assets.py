"""Asset use cases: create for an owner and resolve a permission-checked file.

Permission lives here rather than in the route. An asset is readable by its owner,
or by anyone when it belongs to a published Explore post. A denied read answers
`404 not_found`, so an outsider cannot tell an existing asset from a missing one.

The storage adapter is passed in rather than built from settings, so tests use a
temporary directory and the route owns the app-wide instance.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.problems import ApiError, ErrorCode
from app.core.ids import uuid7
from app.db.models import Asset, Post
from app.db.models.asset import ASSET_KINDS
from app.db.session import session_scope
from app.storage.base import Storage, StorageError, StoredFile, storage_key_for

ASSET_NOT_FOUND_DETAIL = "That asset does not exist."
INVALID_ASSET_KIND_DETAIL = "kind must be image or video."
INVALID_ASSET_DETAIL = "The asset content is not valid."

#: An owner's bytes are private to their browser. A published gallery asset is
#: safe to share in a public cache; the 24-hour window is the documented policy.
OWNER_CACHE_CONTROL = "private, max-age=3600"
PUBLIC_CACHE_CONTROL = "public, max-age=86400"

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


def can_read(asset: Asset, *, user_id: UUID | None, published_post: bool = False) -> bool:
    """Whether the caller may download the bytes.

    The owner always may. Everyone else, including a signed-out caller, may read
    the asset only when a published Explore post attaches it. `published_post` is
    resolved by the caller because that needs the database.
    """
    if user_id is not None and asset.user_id == user_id:
        return True
    return published_post


async def _is_published_post_asset(session: AsyncSession, asset: Asset) -> bool:
    """Whether a published post covers or attaches this asset.

    `posts.asset_id` is the canonical cover link; `assets.post_id` is the
    back-reference for another asset attached to the same post. Either makes the
    bytes public, because both mean "this file is part of a published post".
    """
    statement = (
        select(Post.id)
        .where(
            Post.published.is_(True),
            or_(Post.asset_id == asset.id, Post.id == asset.post_id),
        )
        .limit(1)
    )
    return await session.scalar(statement) is not None


async def create_asset(
    session_factory: async_sessionmaker[AsyncSession],
    storage: Storage,
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
    async with session_scope(session_factory) as session:
        return await add_asset(
            session,
            storage,
            user_id=user_id,
            kind=kind,
            mime_type=mime_type,
            data=data,
            width=width,
            height=height,
            duration_ms=duration_ms,
            generation_id=generation_id,
            post_id=post_id,
        )


async def add_asset(
    session: AsyncSession,
    storage: Storage,
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
    """Insert an Asset row in the caller's transaction.

    The file is still written before the insert, so the same orphan rule holds:
    a failed transaction leaves a file no row names, never a row without bytes.
    The worker uses this so its outputs, the Credit commit, and `succeeded` land
    in one transaction.
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
    stored = await storage.write(key, data, content_type=mime_type)

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
    storage: Storage,
    *,
    asset_id: UUID,
    user_id: UUID | None,
) -> StoredAsset:
    """Return an asset the caller may read, or raise the documented 404.

    A missing row, another owner's row, a missing file, a corrupt key, and a
    denied public read are all `404`: each of them must look identical from
    outside.
    """
    published_post = False
    async with session_scope(session_factory) as session:
        asset = await session.get(Asset, asset_id)
        if asset is None:
            raise _not_found()
        is_owner = user_id is not None and asset.user_id == user_id
        if not is_owner:
            published_post = await _is_published_post_asset(session, asset)
        if not can_read(asset, user_id=user_id, published_post=published_post):
            raise _not_found()
    try:
        stored = await storage.stat(asset.storage_key)
    except (OSError, StorageError):
        raise _not_found() from None
    return StoredAsset(
        asset=asset,
        file=stored,
        cache_control=PUBLIC_CACHE_CONTROL if published_post else OWNER_CACHE_CONTROL,
    )


def _not_found() -> ApiError:
    return ApiError(ErrorCode.NOT_FOUND, 404, detail=ASSET_NOT_FOUND_DETAIL)
