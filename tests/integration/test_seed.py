"""Integration tests for the bundled Explore seed.

The seed is the only writer of the gallery, so these tests treat it as a
contract: sixteen published posts, a stable order, media that is actually served,
and a rerun that changes nothing a client can see while restoring edited fixture
content.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.db.explore_seed import (
    ASSETS_BY_ID,
    MEDIA_BY_SLUG,
    POST_SEEDS,
    POSTS_BY_ID,
    SEED_MEDIA_DIR,
    SYSTEM_AUTHOR_DISPLAY_NAME,
    SYSTEM_AUTHOR_EMAIL,
    SYSTEM_AUTHOR_ID,
    ExploreSeedResult,
    upsert_explore,
)
from app.db.models import Asset, Post, PostLike, User
from app.db.seed import IMAGE_MODEL_ID, VIDEO_MODEL_ID, upsert_models
from app.db.session import create_session_factory, session_scope
from app.main import create_app
from app.storage.local import LocalStorage
from tests.support import asgi_client, assert_problem, make_settings, post_signup

CONFIGURED_CHAT = {
    "ai_base_url": "https://gateway.example.test/v1",
    "ai_api_key": "test-key-not-a-real-secret",
}


@dataclass
class SeedEnv:
    http: httpx.AsyncClient
    settings: Settings
    session_factory: async_sessionmaker[AsyncSession]
    storage: LocalStorage


async def run_seed(env: SeedEnv) -> ExploreSeedResult:
    """Mirror `python -m app.cli seed` without touching the process environment."""
    async with session_scope(env.session_factory) as session:
        await upsert_models(session, env.settings)
        return await upsert_explore(
            session,
            storage=env.storage,
            chat_model_id=env.settings.ai_chat_model,
        )


@pytest.fixture
async def seed_env(
    migrated_database: str,
    engine: AsyncEngine,
    tmp_path: Path,
) -> AsyncIterator[SeedEnv]:
    settings = make_settings(**CONFIGURED_CHAT, storage_dir=tmp_path / "storage")
    factory = create_session_factory(engine)
    storage = LocalStorage(settings.storage_dir)
    # The gallery fixture owns the global posts table, so start it empty and
    # leave it empty; other suites assert their own listings.
    async with session_scope(factory) as session:
        await session.execute(delete(Post))

    async with asgi_client(create_app(settings)) as http:
        signup = await post_signup(http, f"seed-{uuid4().hex}@example.com")
        assert signup.status_code == 201, signup.text
        env = SeedEnv(
            http=http,
            settings=settings,
            session_factory=factory,
            storage=storage,
        )
        await run_seed(env)
        yield env

    async with session_scope(factory) as session:
        await session.execute(delete(Post))
        await session.execute(delete(Asset).where(Asset.id.in_(list(ASSETS_BY_ID))))


def expected_order() -> list[UUID]:
    return [
        post_id
        for post_id, post in sorted(
            POSTS_BY_ID.items(), key=lambda item: item[1].created_at, reverse=True
        )
    ]


def expected_model_id(post_id: UUID, settings: Settings) -> str:
    seed = POSTS_BY_ID[post_id]
    if seed.modality == "chat":
        return settings.ai_chat_model
    if seed.modality == "video":
        return VIDEO_MODEL_ID
    return IMAGE_MODEL_ID


async def test_the_seed_publishes_the_whole_gallery(seed_env: SeedEnv) -> None:
    response = await seed_env.http.get("/v1/explore/posts", params={"limit": 100})

    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == len(POST_SEEDS) == 16
    assert [item["id"] for item in items] == [str(post_id) for post_id in expected_order()]

    modalities = [item["modality"] for item in items]
    assert modalities.count("image") == 10
    assert modalities.count("video") == 4
    assert modalities.count("chat") == 2

    for item in items:
        post_id = UUID(item["id"])
        seed = POSTS_BY_ID[post_id]
        assert item["title"] == seed.title
        assert item["prompt"] == seed.prompt
        assert item["modelId"] == expected_model_id(post_id, seed_env.settings)
        assert item["author"] == {
            "id": str(SYSTEM_AUTHOR_ID),
            "displayName": SYSTEM_AUTHOR_DISPLAY_NAME,
            "initials": "NG",
        }
        assert item["likeCount"] == 0
        assert item["likedByMe"] is False
        if seed.asset_slug is None:
            assert item["asset"] is None
            continue
        media = MEDIA_BY_SLUG[seed.asset_slug]
        assert item["asset"]["mimeType"] == media.mime_type
        assert item["asset"]["width"] == media.width
        assert item["asset"]["height"] == media.height
        assert item["asset"]["durationMs"] == media.duration_ms


async def test_seeded_media_is_served_anonymously(seed_env: SeedEnv) -> None:
    listed = (await seed_env.http.get("/v1/explore/posts?limit=100")).json()["items"]

    async with asgi_client(create_app(seed_env.settings)) as anonymous:
        for item in listed:
            seed = POSTS_BY_ID[UUID(item["id"])]
            if seed.asset_slug is None:
                continue
            media = MEDIA_BY_SLUG[seed.asset_slug]
            response = await anonymous.get(item["asset"]["url"])

            assert response.status_code == 200, item["asset"]["url"]
            assert response.headers["content-type"] == media.mime_type
            assert response.headers["cache-control"] == "public, max-age=86400"
            assert response.content == (SEED_MEDIA_DIR / media.file_name).read_bytes()


async def test_reseeding_is_idempotent(seed_env: SeedEnv) -> None:
    before = (await seed_env.http.get("/v1/explore/posts?limit=100")).json()["items"]

    result = await run_seed(seed_env)

    after = (await seed_env.http.get("/v1/explore/posts?limit=100")).json()["items"]

    assert result.posts == 16
    assert result.assets == 14
    assert [item["id"] for item in after] == [item["id"] for item in before]
    async with seed_env.session_factory() as session:
        posts = await session.scalar(
            select(func.count()).select_from(Post).where(Post.author_id == SYSTEM_AUTHOR_ID)
        )
        assets = await session.scalar(
            select(func.count()).select_from(Asset).where(Asset.user_id == SYSTEM_AUTHOR_ID)
        )
    assert posts == 16
    assert assets == 14


async def test_reseeding_preserves_likes(seed_env: SeedEnv) -> None:
    post_id = expected_order()[0]
    assert (await seed_env.http.post(f"/v1/explore/posts/{post_id}/like")).status_code == 204

    await run_seed(seed_env)

    detail = (await seed_env.http.get(f"/v1/explore/posts/{post_id}")).json()
    assert detail["likeCount"] == 1
    assert detail["likedByMe"] is True
    async with seed_env.session_factory() as session:
        likes = await session.scalar(
            select(func.count()).select_from(PostLike).where(PostLike.post_id == post_id)
        )
    assert likes == 1


async def test_reseeding_restores_edited_fixture_content(seed_env: SeedEnv) -> None:
    post_id = expected_order()[-1]
    seed = POSTS_BY_ID[post_id]
    async with session_scope(seed_env.session_factory) as session:
        row = await session.get(Post, post_id)
        assert row is not None
        row.title = "Edited out of band"
        row.published = False
        row.params = {"aspectRatio": "9:16"}

    await run_seed(seed_env)

    detail = await seed_env.http.get(f"/v1/explore/posts/{post_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["title"] == seed.title
    assert detail.json()["params"] == seed.params
    assert detail.json()["likedByMe"] is False


async def test_reseeding_leaves_another_accounts_post_alone(seed_env: SeedEnv) -> None:
    account_id = (await seed_env.http.get("/v1/auth/me")).json()["user"]["id"]
    async with session_scope(seed_env.session_factory) as session:
        post = Post(
            author_id=UUID(account_id),
            modality="image",
            title="Not part of the fixture",
            prompt="A user-authored row that the seed must not touch",
            model_id=IMAGE_MODEL_ID,
            params={},
            published=True,
        )
        session.add(post)
        await session.flush()
        post_id = post.id

    await run_seed(seed_env)

    async with seed_env.session_factory() as session:
        found = await session.get(Post, post_id)
    assert found is not None
    assert found.title == "Not part of the fixture"
    assert found.published is True


async def test_the_system_author_cannot_be_reached_through_auth(seed_env: SeedEnv) -> None:
    async with asgi_client(create_app(seed_env.settings)) as anonymous:
        login = await anonymous.post(
            "/v1/auth/login",
            json={"email": SYSTEM_AUTHOR_EMAIL, "password": "not the seed password"},
        )
        signup = await anonymous.post(
            "/v1/auth/signup",
            json={
                "email": SYSTEM_AUTHOR_EMAIL,
                "password": "correct horse battery",
                "displayName": "Impostor",
            },
        )

    # The reserved `.invalid` domain fails email validation before any credential
    # or uniqueness check, so no client can claim the seed author's address.
    assert_problem(login, 400, "invalid_request")
    assert_problem(signup, 400, "invalid_request")


async def test_reseeding_restores_the_system_author_display_name(seed_env: SeedEnv) -> None:
    async with session_scope(seed_env.session_factory) as session:
        author = await session.get(User, SYSTEM_AUTHOR_ID)
        assert author is not None
        author.display_name = "Renamed by hand"

    await run_seed(seed_env)

    async with seed_env.session_factory() as session:
        author = await session.get(User, SYSTEM_AUTHOR_ID)
    assert author is not None
    assert author.display_name == SYSTEM_AUTHOR_DISPLAY_NAME


async def test_the_seed_refuses_a_reserved_email_claimed_by_another_account(
    seed_env: SeedEnv,
) -> None:
    impostor_id = uuid4()
    async with session_scope(seed_env.session_factory) as session:
        # Deleting the author would keep the gallery only if the posts and their
        # cover assets go first; `posts.asset_id` is `RESTRICT` on purpose.
        await session.execute(delete(Post))
        await session.execute(delete(Asset).where(Asset.user_id == SYSTEM_AUTHOR_ID))
        await session.execute(delete(User).where(User.id == SYSTEM_AUTHOR_ID))
        session.add(
            User(
                id=impostor_id,
                email=SYSTEM_AUTHOR_EMAIL,
                display_name="Impostor",
                password_hash="unused-in-this-test",
            )
        )

    with pytest.raises(RuntimeError, match="seed author email"):
        await run_seed(seed_env)

    async with session_scope(seed_env.session_factory) as session:
        await session.execute(delete(User).where(User.id == impostor_id))


async def test_the_seeded_assets_belong_to_the_system_author(seed_env: SeedEnv) -> None:
    listed = (await seed_env.http.get("/v1/explore/posts?limit=100")).json()["items"]

    async with seed_env.session_factory() as session:
        for item in listed:
            asset = item["asset"]
            if asset is None:
                continue
            row = await session.get(Asset, UUID(asset["id"]))
            assert row is not None
            assert row.user_id == SYSTEM_AUTHOR_ID
            assert row.post_id is None
            assert row.storage_key.startswith(f"assets/{SYSTEM_AUTHOR_ID}/")
            assert (
                row.size_bytes == (seed_env.settings.storage_dir / row.storage_key).stat().st_size
            )
