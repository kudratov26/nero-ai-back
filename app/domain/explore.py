"""Explore use cases: browse published posts and like or unlike one.

Reads are public. `liked_by_me` is computed only when a session is present, and a
signed-out caller simply gets `false`. Writes require an account, but the post
itself is not owned by the caller: any signed-in account may like any published
post.

An unknown post and an unpublished post are both `404 not_found`, so an outsider
cannot use a like request to discover drafts. Like and unlike are idempotent:
a repeat like is absorbed by the `(post_id, user_id)` primary key, and a repeat
unlike deletes zero rows. Both answer `204`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, func, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.problems import ApiError, ErrorCode
from app.core.cursor import InvalidCursorError, decode_keyset, encode_keyset
from app.db.models import Asset, Post, PostLike, User
from app.db.session import session_scope

POST_PAGE_SIZE = 20
POST_MAX_PAGE_SIZE = 100

POST_NOT_FOUND_DETAIL = "That post does not exist."
INVALID_CURSOR_DETAIL = "The cursor is not valid."


@dataclass(frozen=True)
class PostAuthorView:
    """The public fields of a post's author."""

    id: UUID
    display_name: str
    initials: str


@dataclass(frozen=True)
class PostAssetView:
    """The delivery fields a gallery client needs for a post's media."""

    id: UUID
    mime_type: str
    width: int | None
    height: int | None
    duration_ms: int | None


@dataclass(frozen=True)
class PostView:
    """A post plus the derived values the response needs."""

    post: Post
    author: PostAuthorView | None
    asset: PostAssetView | None
    like_count: int
    liked_by_me: bool


def initials_for(display_name: str) -> str:
    """Return avatar initials for a display name.

    Seed data and accounts both have a display name; a blank one is not possible
    because the users table rejects it, but the fallback keeps this total.
    """
    parts = display_name.split()
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return f"{parts[0][0]}{parts[-1][0]}".upper()


def _decode_post_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        return decode_keyset(cursor)
    except InvalidCursorError as exc:
        raise ApiError(ErrorCode.INVALID_REQUEST, 400, detail=INVALID_CURSOR_DETAIL) from exc


def _not_found() -> ApiError:
    return ApiError(ErrorCode.NOT_FOUND, 404, detail=POST_NOT_FOUND_DETAIL)


async def _views_for(
    session: AsyncSession,
    posts: Sequence[Post],
    *,
    user_id: UUID | None,
) -> list[PostView]:
    """Load authors, assets, counts, and the caller's likes for one page.

    This is deliberately a few bounded queries rather than one wide join: the
    count and the caller's like are separate concerns, and each query is scoped
    to the ids on this page.
    """
    if not posts:
        return []

    post_ids = [post.id for post in posts]
    author_ids = {post.author_id for post in posts if post.author_id is not None}
    asset_ids = {post.asset_id for post in posts if post.asset_id is not None}

    authors: dict[UUID, str] = {}
    if author_ids:
        author_rows = await session.execute(
            select(User.id, User.display_name).where(User.id.in_(author_ids))
        )
        authors = dict(author_rows.all())

    assets: dict[UUID, PostAssetView] = {}
    if asset_ids:
        asset_rows = await session.execute(
            select(
                Asset.id,
                Asset.mime_type,
                Asset.width,
                Asset.height,
                Asset.duration_ms,
            ).where(Asset.id.in_(asset_ids))
        )
        assets = {
            asset_id: PostAssetView(
                id=asset_id,
                mime_type=mime_type,
                width=width,
                height=height,
                duration_ms=duration_ms,
            )
            for asset_id, mime_type, width, height, duration_ms in asset_rows.all()
        }

    count_rows = await session.execute(
        select(PostLike.post_id, func.count())
        .where(PostLike.post_id.in_(post_ids))
        .group_by(PostLike.post_id)
    )
    like_counts = dict(count_rows.all())

    liked: set[UUID] = set()
    if user_id is not None:
        liked_rows = await session.scalars(
            select(PostLike.post_id).where(
                PostLike.post_id.in_(post_ids),
                PostLike.user_id == user_id,
            )
        )
        liked = set(liked_rows)

    views: list[PostView] = []
    for post in posts:
        author = None
        if post.author_id is not None and post.author_id in authors:
            display_name = authors[post.author_id]
            author = PostAuthorView(
                id=post.author_id,
                display_name=display_name,
                initials=initials_for(display_name),
            )
        views.append(
            PostView(
                post=post,
                author=author,
                asset=assets.get(post.asset_id) if post.asset_id is not None else None,
                like_count=like_counts.get(post.id, 0),
                liked_by_me=post.id in liked,
            )
        )
    return views


async def list_posts(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: UUID | None,
    modality: str | None,
    model_id: str | None,
    cursor: str | None,
    limit: int,
) -> tuple[list[PostView], str | None]:
    """Return one page of published posts, newest first.

    The keyset is `(created_at, id)` descending, so two posts created in the same
    millisecond still have a stable total order. Filters are applied alongside the
    cursor, so a client must reuse the same filters when following `nextCursor`;
    the cursor is a position in one ordering, not a snapshot.
    """
    statement = (
        select(Post)
        .where(Post.published.is_(True))
        .order_by(Post.created_at.desc(), Post.id.desc())
        .limit(limit + 1)
    )
    if modality is not None:
        statement = statement.where(Post.modality == modality)
    if model_id is not None:
        statement = statement.where(Post.model_id == model_id)
    if cursor is not None:
        after = _decode_post_cursor(cursor)
        statement = statement.where(tuple_(Post.created_at, Post.id) < after)

    async with session_scope(session_factory) as session:
        rows = list(await session.scalars(statement))
        has_more = len(rows) > limit
        page = rows[:limit]
        views = await _views_for(session, page, user_id=user_id)

    next_cursor = encode_keyset(page[-1].created_at, page[-1].id) if has_more and page else None
    return views, next_cursor


async def get_post(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    post_id: UUID,
    user_id: UUID | None,
) -> PostView:
    """Return one published post, or `404 not_found`."""
    async with session_scope(session_factory) as session:
        post = await session.scalar(
            select(Post).where(Post.id == post_id, Post.published.is_(True))
        )
        if post is None:
            raise _not_found()
        views = await _views_for(session, [post], user_id=user_id)
        return views[0]


async def _require_published(session: AsyncSession, post_id: UUID) -> None:
    found = await session.scalar(
        select(Post.id).where(Post.id == post_id, Post.published.is_(True))
    )
    if found is None:
        raise _not_found()


async def like_post(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    post_id: UUID,
    user_id: UUID,
) -> None:
    """Like a published post. A second like is a no-op, not a conflict."""
    async with session_scope(session_factory) as session:
        await _require_published(session, post_id)
        statement = (
            pg_insert(PostLike)
            .values(post_id=post_id, user_id=user_id)
            .on_conflict_do_nothing(index_elements=[PostLike.post_id, PostLike.user_id])
        )
        await session.execute(statement)


async def unlike_post(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    post_id: UUID,
    user_id: UUID,
) -> None:
    """Remove a like from a published post. Removing a missing like is a no-op."""
    async with session_scope(session_factory) as session:
        await _require_published(session, post_id)
        await session.execute(
            delete(PostLike).where(
                PostLike.post_id == post_id,
                PostLike.user_id == user_id,
            )
        )
