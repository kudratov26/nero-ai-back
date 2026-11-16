"""Integration tests for Explore: public browsing and authenticated likes.

The gallery is global, so this suite starts each test from an empty `posts` table.
That keeps the ordering and cursor assertions independent of seed or asset tests
that ran earlier in the session.

The cases that matter are the contract boundaries: a signed-out reader can list
and read, `likedByMe` reflects only the caller, a duplicate like is a no-op, and
an unpublished or unknown post is hidden behind the same `404`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.ids import uuid7
from app.db.models import Asset, Post, PostLike
from app.db.seed import IMAGE_MODEL_ID, VIDEO_MODEL_ID, upsert_models
from app.db.session import create_session_factory, session_scope
from app.main import create_app
from tests.support import asgi_client, assert_problem, make_settings, post_signup

CONFIGURED_CHAT = {
    "ai_base_url": "https://gateway.example.test/v1",
    "ai_api_key": "test-key-not-a-real-secret",
}
POST_FIELDS = {
    "id",
    "modality",
    "title",
    "prompt",
    "modelId",
    "params",
    "author",
    "asset",
    "likeCount",
    "likedByMe",
    "createdAt",
}


@dataclass
class ExploreEnv:
    http: httpx.AsyncClient
    settings: Settings
    session_factory: async_sessionmaker[AsyncSession]
    user_id: UUID


async def _clear_posts(factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_scope(factory) as session:
        await session.execute(delete(Post))


@pytest.fixture
async def explore_env(
    migrated_database: str,
    engine: AsyncEngine,
    tmp_path: Path,
) -> AsyncIterator[ExploreEnv]:
    settings = make_settings(**CONFIGURED_CHAT, storage_dir=tmp_path / "storage")
    factory = create_session_factory(engine)
    async with session_scope(factory) as session:
        await upsert_models(session, settings)
    await _clear_posts(factory)

    async with asgi_client(create_app(settings)) as http:
        signup = await post_signup(http, f"explore-{uuid4().hex}@example.com")
        assert signup.status_code == 201, signup.text
        yield ExploreEnv(
            http=http,
            settings=settings,
            session_factory=factory,
            user_id=UUID(signup.json()["user"]["id"]),
        )
    await _clear_posts(factory)


async def add_post(env: ExploreEnv, **overrides: Any) -> Post:
    values: dict[str, Any] = {
        "author_id": env.user_id,
        "asset_id": None,
        "modality": "image",
        "title": "A gallery post",
        "prompt": "A lighthouse at dusk",
        "model_id": IMAGE_MODEL_ID,
        "params": {"aspectRatio": "4:5"},
        "published": True,
    }
    values.update(overrides)
    async with session_scope(env.session_factory) as session:
        post = Post(**values)
        session.add(post)
        await session.flush()
        return post


async def add_asset(env: ExploreEnv, *, user_id: UUID | None = None) -> Asset:
    owner = user_id or env.user_id
    asset_id = uuid7()
    async with session_scope(env.session_factory) as session:
        asset = Asset(
            id=asset_id,
            user_id=owner,
            kind="image",
            storage_key=f"assets/{owner}/{asset_id}.png",
            mime_type="image/png",
            size_bytes=1024,
            width=600,
            height=800,
        )
        session.add(asset)
        await session.flush()
        return asset


async def like(http: httpx.AsyncClient, post_id: UUID) -> httpx.Response:
    return await http.post(f"/v1/explore/posts/{post_id}/like")


async def unlike(http: httpx.AsyncClient, post_id: UUID) -> httpx.Response:
    return await http.delete(f"/v1/explore/posts/{post_id}/like")


async def walk_pages(
    http: httpx.AsyncClient,
    *,
    limit: int,
    **filters: str,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(100):
        params: dict[str, str | int] = {"limit": limit, **filters}
        if cursor is not None:
            params["cursor"] = cursor
        response = await http.get("/v1/explore/posts", params=params)
        assert response.status_code == 200, response.text
        page = response.json()
        items.extend(page["items"])
        cursor = page["nextCursor"]
        if cursor is None:
            return items
    raise AssertionError("the cursor never terminated")


# --- Public read -------------------------------------------------------------


async def test_the_gallery_is_public_and_returns_the_documented_shape(
    explore_env: ExploreEnv,
) -> None:
    asset = await add_asset(explore_env)
    post = await add_post(explore_env, asset_id=asset.id)

    async with asgi_client(create_app(explore_env.settings)) as anonymous:
        response = await anonymous.get("/v1/explore/posts")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["nextCursor"] is None
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert set(item) == POST_FIELDS
    assert item["id"] == str(post.id)
    assert item["modality"] == "image"
    assert item["title"] == "A gallery post"
    assert item["prompt"] == "A lighthouse at dusk"
    assert item["modelId"] == IMAGE_MODEL_ID
    assert item["params"] == {"aspectRatio": "4:5"}
    assert item["author"] == {
        "id": str(explore_env.user_id),
        "displayName": "Creator",
        "initials": "CR",
    }
    assert item["asset"] == {
        "id": str(asset.id),
        "url": f"/v1/assets/{asset.id}",
        "mimeType": "image/png",
        "width": 600,
        "height": 800,
        "durationMs": None,
    }
    assert item["likeCount"] == 0
    assert item["likedByMe"] is False
    assert item["createdAt"].endswith("Z")


async def test_a_post_can_have_no_author_and_no_asset(explore_env: ExploreEnv) -> None:
    post = await add_post(
        explore_env,
        author_id=None,
        asset_id=None,
        modality="chat",
        model_id=explore_env.settings.ai_chat_model,
    )

    response = await explore_env.http.get(f"/v1/explore/posts/{post.id}")

    assert response.status_code == 200, response.text
    assert response.json()["author"] is None
    assert response.json()["asset"] is None


async def test_an_unknown_or_unpublished_post_is_404(explore_env: ExploreEnv) -> None:
    unpublished = await add_post(explore_env, published=False)

    unknown = await explore_env.http.get(f"/v1/explore/posts/{uuid7()}")
    draft = await explore_env.http.get(f"/v1/explore/posts/{unpublished.id}")

    assert_problem(unknown, 404, "not_found")
    assert_problem(draft, 404, "not_found")


async def test_an_invalid_session_cookie_is_treated_as_anonymous(
    explore_env: ExploreEnv,
) -> None:
    await add_post(explore_env)
    cookie = explore_env.settings.session_cookie_name

    async with asgi_client(
        create_app(explore_env.settings),
        headers={"cookie": f"{cookie}=not-a-real-token"},
    ) as stranger:
        response = await stranger.get("/v1/explore/posts")

    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["likedByMe"] is False


# --- Pagination and filters --------------------------------------------------


async def test_pagination_is_newest_first_and_terminates(explore_env: ExploreEnv) -> None:
    base = datetime(2026, 11, 1, 12, tzinfo=UTC)
    created = [
        await add_post(
            explore_env,
            title=f"Post {index}",
            created_at=base + timedelta(minutes=index),
        )
        for index in range(5)
    ]

    items = await walk_pages(explore_env.http, limit=2)

    assert [item["id"] for item in items] == [str(post.id) for post in reversed(created)]
    assert [item["title"] for item in items] == [f"Post {index}" for index in reversed(range(5))]


async def test_the_id_tiebreaker_keeps_equal_timestamps_stable(explore_env: ExploreEnv) -> None:
    created_at = datetime(2026, 11, 1, 12, tzinfo=UTC)
    created = [await add_post(explore_env, created_at=created_at) for _ in range(3)]

    items = await walk_pages(explore_env.http, limit=1)

    assert [item["id"] for item in items] == [
        str(post.id) for post in sorted(created, key=lambda post: post.id, reverse=True)
    ]


async def test_a_malformed_cursor_is_400(explore_env: ExploreEnv) -> None:
    response = await explore_env.http.get(
        "/v1/explore/posts", params={"cursor": "not-a-real-cursor"}
    )

    body = assert_problem(response, 400, "invalid_request")
    assert body["detail"] == "The cursor is not valid."


async def test_modality_and_model_filters_apply_before_pagination(
    explore_env: ExploreEnv,
) -> None:
    image = await add_post(explore_env, modality="image", model_id=IMAGE_MODEL_ID)
    video = await add_post(explore_env, modality="video", model_id=VIDEO_MODEL_ID)
    chat = await add_post(
        explore_env,
        modality="chat",
        model_id=explore_env.settings.ai_chat_model,
    )

    images = (await explore_env.http.get("/v1/explore/posts?modality=image")).json()
    videos = (await explore_env.http.get("/v1/explore/posts?modelId=video-demo")).json()
    both = (
        await explore_env.http.get("/v1/explore/posts?modality=image&modelId=video-demo")
    ).json()
    all_posts = (await explore_env.http.get("/v1/explore/posts")).json()

    assert {item["id"] for item in all_posts["items"]} == {
        str(image.id),
        str(video.id),
        str(chat.id),
    }
    assert [item["id"] for item in images["items"]] == [str(image.id)]
    assert [item["id"] for item in videos["items"]] == [str(video.id)]
    assert both["items"] == []


@pytest.mark.parametrize("limit", [0, 101])
async def test_the_limit_is_bounded(explore_env: ExploreEnv, limit: int) -> None:
    response = await explore_env.http.get("/v1/explore/posts", params={"limit": limit})

    assert_problem(response, 400, "invalid_request")


# --- Likes -------------------------------------------------------------------


async def test_like_and_unlike_are_idempotent(explore_env: ExploreEnv) -> None:
    post = await add_post(explore_env)

    first_like = await like(explore_env.http, post.id)
    second_like = await like(explore_env.http, post.id)
    after_like = await explore_env.http.get(f"/v1/explore/posts/{post.id}")

    assert first_like.status_code == 204
    assert second_like.status_code == 204
    assert first_like.content == b""
    assert after_like.json()["likeCount"] == 1
    assert after_like.json()["likedByMe"] is True

    first_unlike = await unlike(explore_env.http, post.id)
    second_unlike = await unlike(explore_env.http, post.id)
    after_unlike = await explore_env.http.get(f"/v1/explore/posts/{post.id}")

    assert first_unlike.status_code == 204
    assert second_unlike.status_code == 204
    assert after_unlike.json()["likeCount"] == 0
    assert after_unlike.json()["likedByMe"] is False

    async with explore_env.session_factory() as session:
        count = await session.scalar(select(func.count()).select_from(PostLike))
    assert count == 0


async def test_likes_are_counted_per_account_and_never_leak(explore_env: ExploreEnv) -> None:
    post = await add_post(explore_env)

    async with asgi_client(create_app(explore_env.settings)) as other:
        signup = await post_signup(other, f"other-{uuid4().hex}@example.com")
        assert signup.status_code == 201, signup.text

        assert (await like(other, post.id)).status_code == 204
        assert (await like(explore_env.http, post.id)).status_code == 204

        mine = (await explore_env.http.get(f"/v1/explore/posts/{post.id}")).json()
        theirs = (await other.get(f"/v1/explore/posts/{post.id}")).json()
        assert mine["likeCount"] == 2
        assert theirs["likeCount"] == 2
        assert mine["likedByMe"] is True
        assert theirs["likedByMe"] is True

        assert (await unlike(other, post.id)).status_code == 204

        mine = (await explore_env.http.get(f"/v1/explore/posts/{post.id}")).json()
        theirs = (await other.get(f"/v1/explore/posts/{post.id}")).json()
        assert mine["likeCount"] == 1
        assert mine["likedByMe"] is True
        assert theirs["likeCount"] == 1
        assert theirs["likedByMe"] is False


async def test_a_signed_out_reader_gets_the_count_but_not_liked_by_me(
    explore_env: ExploreEnv,
) -> None:
    post = await add_post(explore_env)
    assert (await like(explore_env.http, post.id)).status_code == 204

    async with asgi_client(create_app(explore_env.settings)) as anonymous:
        listing = (await anonymous.get("/v1/explore/posts")).json()
        detail = (await anonymous.get(f"/v1/explore/posts/{post.id}")).json()

    assert listing["items"][0]["likeCount"] == 1
    assert listing["items"][0]["likedByMe"] is False
    assert detail["likeCount"] == 1
    assert detail["likedByMe"] is False


async def test_liking_requires_a_session(explore_env: ExploreEnv) -> None:
    post = await add_post(explore_env)

    async with asgi_client(create_app(explore_env.settings)) as anonymous:
        response = await like(anonymous, post.id)

    assert_problem(response, 401, "unauthorized")


@pytest.mark.parametrize("method", ["post", "delete"])
async def test_liking_an_unknown_or_unpublished_post_is_404(
    explore_env: ExploreEnv,
    method: str,
) -> None:
    unpublished = await add_post(explore_env, published=False)

    unknown = await getattr(explore_env.http, method)(f"/v1/explore/posts/{uuid7()}/like")
    draft = await getattr(explore_env.http, method)(f"/v1/explore/posts/{unpublished.id}/like")

    assert_problem(unknown, 404, "not_found")
    assert_problem(draft, 404, "not_found")


async def test_publishing_a_post_makes_it_visible_and_likeable(explore_env: ExploreEnv) -> None:
    post = await add_post(explore_env, published=False)
    assert (await explore_env.http.get(f"/v1/explore/posts/{post.id}")).status_code == 404

    async with session_scope(explore_env.session_factory) as session:
        row = await session.get(Post, post.id)
        assert row is not None
        row.published = True

    assert (await explore_env.http.get(f"/v1/explore/posts/{post.id}")).status_code == 200
    assert (await like(explore_env.http, post.id)).status_code == 204
