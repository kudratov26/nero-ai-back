"""The bundled Explore gallery and its idempotent seed step.

The gallery is curated: users cannot publish a Generation in v1, so the only
writer is this module, called by `python -m app.cli seed`. Sixteen fixtures match
the frontend demo's variety (ten images, four videos, two text posts). Each image
and video post points at a small bundled file under `seed_media/`, which is
copied into the configured storage root.

Ids are derived with `uuid5` from a fixed namespace, so a reseed updates the same
rows and rewrites the same files instead of duplicating the gallery. Posts keep
their fixture `created_at`, which keeps the Explore ordering stable across
reseeds, and likes are untouched because the post ids never change.

Every seeded row is owned by one system author. That account is created with a
random password that is never printed, and its email uses the reserved
`.invalid` TLD, so it cannot receive mail or be reached through signup.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.db.models import Asset, Post, User
from app.db.seed import IMAGE_MODEL_ID, VIDEO_MODEL_ID
from app.storage.base import Storage, storage_key_for

#: Bundled media shipped next to this module, so the Docker image (which copies
#: `app/`) has everything the seed needs.
SEED_MEDIA_DIR = Path(__file__).resolve().parent / "seed_media"

SYSTEM_AUTHOR_SLUG = "gallery"
SYSTEM_AUTHOR_EMAIL = "gallery@nero.invalid"
SYSTEM_AUTHOR_DISPLAY_NAME = "Nero Gallery"

#: Stable namespace for seeded identifiers. Do not change it: every seeded id
#: and therefore every published asset URL is derived from it.
_SEED_NAMESPACE = uuid5(NAMESPACE_URL, "https://nero.ai/seed/explore")


def seed_uuid(kind: str, slug: str) -> UUID:
    """Return the stable id for one seeded row."""
    return uuid5(_SEED_NAMESPACE, f"{kind}/{slug}")


SYSTEM_AUTHOR_ID = seed_uuid("user", SYSTEM_AUTHOR_SLUG)


@dataclass(frozen=True)
class MediaSeed:
    """One bundled file and the asset metadata that describes it."""

    slug: str
    file_name: str
    kind: Literal["image", "video"]
    mime_type: str
    width: int
    height: int
    duration_ms: int | None = None


@dataclass(frozen=True)
class PostSeed:
    """One gallery row. `asset_slug` is null for a text post."""

    slug: str
    modality: Literal["chat", "image", "video"]
    title: str
    prompt: str
    asset_slug: str | None
    params: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime(2026, 10, 1, tzinfo=UTC))


#: Ten images at the three aspect ratios the image model advertises, four clips
#: at the video model's five-second option, and two text posts with no asset.
MEDIA_SEEDS: tuple[MediaSeed, ...] = (
    MediaSeed("blue-hour", "art-01.jpg", "image", "image/jpeg", 720, 900),
    MediaSeed("last-train", "art-02.jpg", "image", "image/jpeg", 720, 900),
    MediaSeed("workshop", "art-03.jpg", "image", "image/jpeg", 720, 900),
    MediaSeed("spring-branches", "art-04.jpg", "image", "image/jpeg", 720, 900),
    MediaSeed("photographer", "art-05.jpg", "image", "image/jpeg", 720, 900),
    MediaSeed("golden-grasses", "art-06.jpg", "image", "image/jpeg", 720, 900),
    MediaSeed("coral-sunset", "art-07.jpg", "image", "image/jpeg", 800, 800),
    MediaSeed("empty-swing", "art-08.jpg", "image", "image/jpeg", 800, 800),
    MediaSeed("sunlit-oranges", "art-09.jpg", "image", "image/jpeg", 960, 540),
    MediaSeed("forest-highway", "art-10.jpg", "image", "image/jpeg", 960, 540),
    MediaSeed("flower-bud", "clip-01.mp4", "video", "video/mp4", 640, 360, 5_000),
    MediaSeed("crowded-room", "clip-02.mp4", "video", "video/mp4", 640, 360, 5_000),
    MediaSeed("night-interior", "clip-03.mp4", "video", "video/mp4", 640, 360, 5_000),
    MediaSeed("title-card", "clip-04.mp4", "video", "video/mp4", 640, 360, 5_000),
)

MEDIA_BY_SLUG: dict[str, MediaSeed] = {media.slug: media for media in MEDIA_SEEDS}


def _at(month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=UTC)


POST_SEEDS: tuple[PostSeed, ...] = (
    PostSeed(
        slug="blue-hour",
        modality="image",
        title="Blue Hour Skyline",
        prompt="A city skyline at blue hour from across the water, long lens, cool film stock",
        asset_slug="blue-hour",
        params={"aspectRatio": "4:5"},
        created_at=_at(10, 18, 14, 20),
    ),
    PostSeed(
        slug="last-train",
        modality="image",
        title="Last Train",
        prompt="A lone commuter on a black-and-white platform, long lens, 35mm grain",
        asset_slug="last-train",
        params={"aspectRatio": "4:5"},
        created_at=_at(10, 17, 9, 5),
    ),
    PostSeed(
        slug="workshop",
        modality="image",
        title="Workshop Tools",
        prompt="A flat lay of well-used workshop tools on dark oak, hard side light",
        asset_slug="workshop",
        params={"aspectRatio": "4:5"},
        created_at=_at(10, 16, 18, 40),
    ),
    PostSeed(
        slug="spring-branches",
        modality="image",
        title="Spring Branches",
        prompt="Flowering branches against a pale rose sky, soft spring light",
        asset_slug="spring-branches",
        params={"aspectRatio": "4:5"},
        created_at=_at(10, 15, 11, 15),
    ),
    PostSeed(
        slug="photographer",
        modality="image",
        title="The Photographer",
        prompt="A photographer with a vintage rangefinder in the garden, candid portrait",
        asset_slug="photographer",
        params={"aspectRatio": "4:5"},
        created_at=_at(10, 14, 21, 30),
    ),
    PostSeed(
        slug="golden-grasses",
        modality="image",
        title="Golden Grasses",
        prompt="Golden grasses catching the last light, shallow focus, warm haze",
        asset_slug="golden-grasses",
        params={"aspectRatio": "4:5"},
        created_at=_at(10, 13, 16, 0),
    ),
    PostSeed(
        slug="coral-sunset",
        modality="image",
        title="Coral Sunset",
        prompt="A coral sunset over a still sea, minimal horizon, 35mm",
        asset_slug="coral-sunset",
        params={"aspectRatio": "1:1"},
        created_at=_at(10, 12, 10, 10),
    ),
    PostSeed(
        slug="empty-swing",
        modality="image",
        title="Empty Swing",
        prompt="An empty swing on a grassy headland at dusk, quiet landscape",
        asset_slug="empty-swing",
        params={"aspectRatio": "1:1"},
        created_at=_at(10, 11, 8, 45),
    ),
    PostSeed(
        slug="sunlit-oranges",
        modality="image",
        title="Sunlit Oranges",
        prompt="Sunlit oranges against a hard blue sky, 35mm, slight overexposure",
        asset_slug="sunlit-oranges",
        params={"aspectRatio": "16:9"},
        created_at=_at(10, 10, 19, 25),
    ),
    PostSeed(
        slug="forest-highway",
        modality="image",
        title="Forest Highway",
        prompt="An aerial view of a forest highway, deep green, patient geometry",
        asset_slug="forest-highway",
        params={"aspectRatio": "16:9"},
        created_at=_at(10, 9, 13, 55),
    ),
    PostSeed(
        slug="flower-bud",
        modality="video",
        title="Flower Bud at Dawn",
        prompt="Macro study of a flower bud opening, shallow depth of field, dawn",
        asset_slug="flower-bud",
        params={"aspectRatio": "16:9", "durationSeconds": 5},
        created_at=_at(10, 18, 7, 40),
    ),
    PostSeed(
        slug="crowded-room",
        modality="video",
        title="Faces Toward the Light",
        prompt=("A black-and-white scene from a crowded room, faces turned toward the light, 16mm"),
        asset_slug="crowded-room",
        params={"aspectRatio": "16:9", "durationSeconds": 5},
        created_at=_at(10, 16, 17, 10),
    ),
    PostSeed(
        slug="night-interior",
        modality="video",
        title="One Window",
        prompt="A night interior in near-darkness, one window, 16mm",
        asset_slug="night-interior",
        params={"aspectRatio": "16:9", "durationSeconds": 5},
        created_at=_at(10, 13, 6, 20),
    ),
    PostSeed(
        slug="title-card",
        modality="video",
        title="Title Card",
        prompt="A film-project title card, cyan type on white, 16mm leader",
        asset_slug="title-card",
        params={"aspectRatio": "16:9", "durationSeconds": 5},
        created_at=_at(10, 11, 15, 35),
    ),
    PostSeed(
        slug="mood-board",
        modality="chat",
        title="Mood Board vs Style Guide",
        prompt=(
            "Explain the difference between a mood board and a style guide, with a short example."
        ),
        asset_slug=None,
        created_at=_at(10, 8, 12, 0),
    ),
    PostSeed(
        slug="second-sky",
        modality="chat",
        title="Second Sky",
        prompt=(
            "Write a six-line poem about a city that only appears after rain. "
            "Avoid the words 'city' and 'rain'."
        ),
        asset_slug=None,
        created_at=_at(10, 7, 20, 15),
    ),
)

POSTS_BY_ID: dict[UUID, PostSeed] = {seed_uuid("post", post.slug): post for post in POST_SEEDS}
ASSETS_BY_ID: dict[UUID, MediaSeed] = {
    seed_uuid("asset", media.slug): media for media in MEDIA_SEEDS
}


@dataclass(frozen=True)
class ExploreSeedResult:
    """What the seed wrote, for the CLI log and the tests."""

    author_id: UUID
    posts: int
    assets: int


def model_id_for(post: PostSeed, *, chat_model_id: str) -> str:
    """The catalog row a fixture points at.

    Text posts use the configured chat model. They are still published when the
    gateway is not configured: the gallery is read-only, and the post payload
    never presents the model as available. The catalog remains the only place
    that answers "can I run this model?".
    """
    if post.modality == "chat":
        return chat_model_id
    if post.modality == "video":
        return VIDEO_MODEL_ID
    return IMAGE_MODEL_ID


def media_bytes(media: MediaSeed) -> bytes:
    """Read one bundled file, with a clear error when the image was built without it."""
    path = SEED_MEDIA_DIR / media.file_name
    try:
        return path.read_bytes()
    except OSError as exc:
        raise FileNotFoundError(f"bundled seed media is missing: {media.file_name}") from exc


async def _ensure_system_author(session: AsyncSession) -> UUID:
    """Create the seed author once and keep its display name fresh.

    A reseed never rotates the password hash: the account cannot sign in either
    way, and rewriting it would only churn the row.
    """
    author = await session.get(User, SYSTEM_AUTHOR_ID)
    if author is not None:
        if author.display_name != SYSTEM_AUTHOR_DISPLAY_NAME:
            author.display_name = SYSTEM_AUTHOR_DISPLAY_NAME
        return SYSTEM_AUTHOR_ID

    taken = await session.scalar(select(User.id).where(User.email == SYSTEM_AUTHOR_EMAIL))
    if taken is not None:
        raise RuntimeError(
            "the seed author email is already used by another account; "
            "remove that account before seeding"
        )

    session.add(
        User(
            id=SYSTEM_AUTHOR_ID,
            email=SYSTEM_AUTHOR_EMAIL,
            display_name=SYSTEM_AUTHOR_DISPLAY_NAME,
            password_hash=hash_password(secrets.token_urlsafe(48)),
        )
    )
    await session.flush()
    return SYSTEM_AUTHOR_ID


async def _upsert_media(session: AsyncSession, storage: Storage, *, author_id: UUID) -> int:
    """Write every bundled file and upsert its asset row."""
    for media in MEDIA_SEEDS:
        data = media_bytes(media)
        asset_id = seed_uuid("asset", media.slug)
        extension = media.file_name.rsplit(".", 1)[1]
        key = storage_key_for(user_id=author_id, asset_id=asset_id, extension=extension)
        await storage.write(key, data, content_type=media.mime_type)

        statement = pg_insert(Asset).values(
            id=asset_id,
            user_id=author_id,
            post_id=None,
            kind=media.kind,
            storage_key=key,
            mime_type=media.mime_type,
            size_bytes=len(data),
            width=media.width,
            height=media.height,
            duration_ms=media.duration_ms,
        )
        statement = statement.on_conflict_do_update(
            index_elements=[Asset.id],
            set_={
                "user_id": statement.excluded.user_id,
                "kind": statement.excluded.kind,
                "storage_key": statement.excluded.storage_key,
                "mime_type": statement.excluded.mime_type,
                "size_bytes": statement.excluded.size_bytes,
                "width": statement.excluded.width,
                "height": statement.excluded.height,
                "duration_ms": statement.excluded.duration_ms,
            },
        )
        await session.execute(statement)
    return len(MEDIA_SEEDS)


async def _upsert_posts(session: AsyncSession, *, author_id: UUID, chat_model_id: str) -> int:
    """Upsert every fixture post, restoring any edited content."""
    for post in POST_SEEDS:
        asset_id = None if post.asset_slug is None else seed_uuid("asset", post.asset_slug)
        statement = pg_insert(Post).values(
            id=seed_uuid("post", post.slug),
            author_id=author_id,
            asset_id=asset_id,
            modality=post.modality,
            title=post.title,
            prompt=post.prompt,
            model_id=model_id_for(post, chat_model_id=chat_model_id),
            params=post.params,
            published=True,
            created_at=post.created_at,
        )
        statement = statement.on_conflict_do_update(
            index_elements=[Post.id],
            set_={
                "author_id": statement.excluded.author_id,
                "asset_id": statement.excluded.asset_id,
                "modality": statement.excluded.modality,
                "title": statement.excluded.title,
                "prompt": statement.excluded.prompt,
                "model_id": statement.excluded.model_id,
                "params": statement.excluded.params,
                "published": statement.excluded.published,
                "created_at": statement.excluded.created_at,
            },
        )
        await session.execute(statement)
    return len(POST_SEEDS)


async def upsert_explore(
    session: AsyncSession,
    *,
    storage: Storage,
    chat_model_id: str,
) -> ExploreSeedResult:
    """Seed the system author, the bundled media, and the sixteen posts.

    Idempotent: fixed ids make every rerun an update. The caller owns the
    transaction, so the gallery commits or rolls back with the catalog. Files are
    written before the commit; a rolled-back run leaves unreferenced bytes that
    the next run overwrites.
    """
    author_id = await _ensure_system_author(session)
    assets = await _upsert_media(session, storage, author_id=author_id)
    posts = await _upsert_posts(session, author_id=author_id, chat_model_id=chat_model_id)
    return ExploreSeedResult(author_id=author_id, posts=posts, assets=assets)
