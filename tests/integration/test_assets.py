"""Integration tests for binary asset delivery.

The route is the only reachable file surface, so the tests cover it end to end:
exact bytes, the delivery headers, a range, and the three ways a caller can be
refused (anonymous, another account, and a corrupt storage row).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.api.problems import ApiError, ErrorCode
from app.core.config import Settings
from app.core.ids import uuid7
from app.db.models import Asset, Model, Post
from app.db.session import create_session_factory, session_scope
from app.domain import assets as asset_service
from app.main import create_app
from app.storage.local import LocalStorage
from tests.support import asgi_client, assert_problem, make_settings, post_signup

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + bytes(range(64))
MP4_BYTES = b"\x00\x00\x00\x18ftypmp42" + bytes(range(64, 128))


@dataclass
class AssetEnv:
    http: httpx.AsyncClient
    settings: Settings
    storage: LocalStorage
    user_id: UUID


@pytest.fixture
async def asset_env(migrated_database: str, tmp_path: Path) -> AsyncIterator[AssetEnv]:
    settings = make_settings(storage_dir=tmp_path / "storage")
    async with asgi_client(create_app(settings)) as http:
        signup = await post_signup(http, f"assets-{uuid4().hex}@example.com")
        assert signup.status_code == 201, signup.text
        yield AssetEnv(
            http=http,
            settings=settings,
            storage=LocalStorage(settings.storage_dir),
            user_id=UUID(signup.json()["user"]["id"]),
        )


async def add_asset(
    engine: AsyncEngine,
    storage: LocalStorage,
    user_id: UUID,
    *,
    data: bytes = PNG_BYTES,
    kind: str = "image",
    mime_type: str = "image/png",
    storage_key: str | None = None,
    write: bool = True,
    **overrides: Any,
) -> Asset:
    asset_id = uuid7()
    key = storage_key or f"assets/{user_id}/{asset_id}.png"
    if write:
        storage.write(key, data)
    async with session_scope(create_session_factory(engine)) as session:
        asset = Asset(
            id=asset_id,
            user_id=user_id,
            kind=kind,
            storage_key=key,
            mime_type=mime_type,
            size_bytes=len(data),
            width=600,
            height=800,
            **overrides,
        )
        session.add(asset)
        await session.flush()
        return asset


POST_MODEL_ID = "assets-post-model"


async def add_post(
    engine: AsyncEngine,
    *,
    author_id: UUID,
    asset_id: UUID | None = None,
    published: bool = True,
) -> Post:
    """Create a post with a private test model, so the catalog is untouched."""
    async with session_scope(create_session_factory(engine)) as session:
        if await session.get(Model, POST_MODEL_ID) is None:
            session.add(
                Model(
                    id=POST_MODEL_ID,
                    name="Asset Post Model",
                    provider="Test",
                    provider_model_id=POST_MODEL_ID,
                    modality="image",
                    description="A model used by the asset permission tests.",
                    badge=None,
                    credit_cost=0,
                    credit_cost_10s=None,
                    capabilities={},
                    context_window=None,
                    available=True,
                    sort_order=0,
                )
            )
            await session.flush()
        post = Post(
            author_id=author_id,
            asset_id=asset_id,
            modality="image",
            title="A published asset",
            prompt="A lighthouse at dusk",
            model_id=POST_MODEL_ID,
            params={},
            published=published,
        )
        session.add(post)
        await session.flush()
        return post


async def test_the_owner_downloads_the_exact_bytes(
    asset_env: AssetEnv, engine: AsyncEngine
) -> None:
    asset = await add_asset(engine, asset_env.storage, asset_env.user_id)

    response = await asset_env.http.get(f"/v1/assets/{asset.id}")

    assert response.status_code == 200, response.text
    assert response.content == PNG_BYTES
    assert response.headers["content-type"] == "image/png"
    assert response.headers["content-length"] == str(len(PNG_BYTES))
    assert response.headers["cache-control"] == "private, max-age=3600"
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["etag"].startswith('"')
    assert "last-modified" in response.headers


async def test_the_etag_is_stable_across_requests(asset_env: AssetEnv, engine: AsyncEngine) -> None:
    asset = await add_asset(engine, asset_env.storage, asset_env.user_id)
    url = f"/v1/assets/{asset.id}"

    first = await asset_env.http.get(url)
    second = await asset_env.http.get(url)

    assert first.headers["etag"] == second.headers["etag"]
    assert first.headers["last-modified"] == second.headers["last-modified"]


async def test_a_range_returns_the_documented_slice(
    asset_env: AssetEnv, engine: AsyncEngine
) -> None:
    asset = await add_asset(engine, asset_env.storage, asset_env.user_id)

    response = await asset_env.http.get(f"/v1/assets/{asset.id}", headers={"range": "bytes=4-9"})

    assert response.status_code == 206
    assert response.content == PNG_BYTES[4:10]
    assert response.headers["content-range"] == f"bytes 4-9/{len(PNG_BYTES)}"
    assert response.headers["content-length"] == "6"
    assert response.headers["accept-ranges"] == "bytes"


async def test_a_video_range_is_served_with_its_mime_type(
    asset_env: AssetEnv, engine: AsyncEngine
) -> None:
    asset = await add_asset(
        engine,
        asset_env.storage,
        asset_env.user_id,
        data=MP4_BYTES,
        kind="video",
        mime_type="video/mp4",
        duration_ms=10_000,
    )

    response = await asset_env.http.get(f"/v1/assets/{asset.id}", headers={"range": "bytes=0-3"})

    assert response.status_code == 206
    assert response.content == MP4_BYTES[:4]
    assert response.headers["content-type"] == "video/mp4"
    assert response.headers["cache-control"] == "private, max-age=3600"


async def test_an_unsatisfiable_range_is_rejected(asset_env: AssetEnv, engine: AsyncEngine) -> None:
    asset = await add_asset(engine, asset_env.storage, asset_env.user_id)

    response = await asset_env.http.get(
        f"/v1/assets/{asset.id}", headers={"range": f"bytes={len(PNG_BYTES) + 10}-"}
    )

    assert response.status_code == 416
    assert response.headers["content-range"] == f"bytes */{len(PNG_BYTES)}"


async def test_an_unknown_asset_is_404(asset_env: AssetEnv) -> None:
    response = await asset_env.http.get(f"/v1/assets/{uuid7()}")

    assert_problem(response, 404, "not_found")


async def test_a_signed_out_reader_gets_404(asset_env: AssetEnv, engine: AsyncEngine) -> None:
    asset = await add_asset(engine, asset_env.storage, asset_env.user_id)

    async with asgi_client(create_app(asset_env.settings)) as anonymous:
        response = await anonymous.get(f"/v1/assets/{asset.id}")

    assert_problem(response, 404, "not_found")


async def test_another_account_gets_404_rather_than_403(
    asset_env: AssetEnv, engine: AsyncEngine
) -> None:
    asset = await add_asset(engine, asset_env.storage, asset_env.user_id)

    async with asgi_client(create_app(asset_env.settings)) as other:
        signup = await post_signup(other, f"stranger-{uuid4().hex}@example.com")
        assert signup.status_code == 201, signup.text
        response = await other.get(f"/v1/assets/{asset.id}")

    assert_problem(response, 404, "not_found")


async def test_an_invalid_session_cookie_is_treated_as_anonymous(
    asset_env: AssetEnv, engine: AsyncEngine
) -> None:
    asset = await add_asset(engine, asset_env.storage, asset_env.user_id)
    cookie = asset_env.settings.session_cookie_name

    async with asgi_client(
        create_app(asset_env.settings), headers={"cookie": f"{cookie}=not-a-real-token"}
    ) as stranger:
        response = await stranger.get(f"/v1/assets/{asset.id}")

    assert_problem(response, 404, "not_found")


async def test_a_missing_file_is_404_not_a_server_error(
    asset_env: AssetEnv, engine: AsyncEngine
) -> None:
    asset = await add_asset(engine, asset_env.storage, asset_env.user_id, write=False)

    response = await asset_env.http.get(f"/v1/assets/{asset.id}")

    assert_problem(response, 404, "not_found")


async def test_a_symlinked_storage_key_cannot_escape_the_root(
    asset_env: AssetEnv,
    engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    """The row passes the database checks; the adapter still refuses the escape."""
    asset_id = uuid7()
    key = f"assets/{asset_env.user_id}/{asset_id}.png"
    destination = asset_env.settings.storage_dir / key
    destination.parent.mkdir(parents=True)
    secret = tmp_path / "secret.png"
    secret.write_bytes(b"a file outside the storage root")
    destination.symlink_to(secret)

    async with session_scope(create_session_factory(engine)) as session:
        session.add(
            Asset(
                id=asset_id,
                user_id=asset_env.user_id,
                kind="image",
                storage_key=key,
                mime_type="image/png",
                size_bytes=secret.stat().st_size,
            )
        )
        await session.flush()

    response = await asset_env.http.get(f"/v1/assets/{asset_id}")

    assert_problem(response, 404, "not_found")


async def test_create_asset_stores_the_bytes_and_persists_the_row(
    asset_env: AssetEnv, engine: AsyncEngine
) -> None:
    asset = await asset_service.create_asset(
        create_session_factory(engine),
        asset_env.storage,
        user_id=asset_env.user_id,
        kind="image",
        mime_type="image/png",
        data=PNG_BYTES,
        width=600,
        height=800,
    )

    assert asset.storage_key == f"assets/{asset_env.user_id}/{asset.id}.png"
    assert asset.size_bytes == len(PNG_BYTES)
    assert asset_env.storage.path_for(asset.storage_key).read_bytes() == PNG_BYTES
    response = await asset_env.http.get(f"/v1/assets/{asset.id}")
    assert response.status_code == 200
    assert response.content == PNG_BYTES


async def test_create_asset_keeps_the_optional_links(
    asset_env: AssetEnv, engine: AsyncEngine
) -> None:
    generation_id = uuid7()
    post = await add_post(engine, author_id=asset_env.user_id, asset_id=None)

    asset = await asset_service.create_asset(
        create_session_factory(engine),
        asset_env.storage,
        user_id=asset_env.user_id,
        kind="video",
        mime_type="video/mp4",
        data=MP4_BYTES,
        duration_ms=10_000,
        generation_id=generation_id,
        post_id=post.id,
    )

    assert asset.generation_id == generation_id
    assert asset.post_id == post.id
    assert asset.duration_ms == 10_000


@pytest.mark.parametrize(
    ("kind", "mime_type", "data"),
    [
        ("audio", "audio/mpeg", b"bytes"),
        ("image", "", b"bytes"),
        ("image", "image/png", b""),
    ],
)
async def test_create_asset_rejects_invalid_input(
    asset_env: AssetEnv,
    engine: AsyncEngine,
    kind: str,
    mime_type: str,
    data: bytes,
) -> None:
    with pytest.raises(ApiError) as raised:
        await asset_service.create_asset(
            create_session_factory(engine),
            asset_env.storage,
            user_id=asset_env.user_id,
            kind=kind,
            mime_type=mime_type,
            data=data,
        )

    assert raised.value.code is ErrorCode.INVALID_REQUEST


@pytest.mark.parametrize(
    ("mime_type", "extension"),
    [
        ("image/png", "png"),
        ("image/jpeg", "jpg"),
        ("video/mp4", "mp4"),
        ("application/octet-stream", "bin"),
    ],
)
def test_extension_for_mime(mime_type: str, extension: str) -> None:
    assert asset_service.extension_for_mime(mime_type) == extension


# --- Published-post access ---------------------------------------------------


async def test_an_asset_on_a_published_post_is_public(
    asset_env: AssetEnv, engine: AsyncEngine
) -> None:
    asset = await add_asset(engine, asset_env.storage, asset_env.user_id)
    await add_post(engine, author_id=asset_env.user_id, asset_id=asset.id)

    async with asgi_client(create_app(asset_env.settings)) as anonymous:
        response = await anonymous.get(f"/v1/assets/{asset.id}")

    assert response.status_code == 200, response.text
    assert response.content == PNG_BYTES
    assert response.headers["cache-control"] == "public, max-age=86400"

    async with asgi_client(create_app(asset_env.settings)) as other:
        signup = await post_signup(other, f"reader-{uuid4().hex}@example.com")
        assert signup.status_code == 201, signup.text
        response = await other.get(f"/v1/assets/{asset.id}")

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "public, max-age=86400"


async def test_the_owner_keeps_the_private_cache_policy_on_a_public_asset(
    asset_env: AssetEnv, engine: AsyncEngine
) -> None:
    asset = await add_asset(engine, asset_env.storage, asset_env.user_id)
    await add_post(engine, author_id=asset_env.user_id, asset_id=asset.id)

    response = await asset_env.http.get(f"/v1/assets/{asset.id}")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, max-age=3600"


async def test_a_public_asset_still_supports_range_requests(
    asset_env: AssetEnv, engine: AsyncEngine
) -> None:
    asset = await add_asset(engine, asset_env.storage, asset_env.user_id)
    await add_post(engine, author_id=asset_env.user_id, asset_id=asset.id)

    async with asgi_client(create_app(asset_env.settings)) as anonymous:
        response = await anonymous.get(f"/v1/assets/{asset.id}", headers={"range": "bytes=0-3"})

    assert response.status_code == 206
    assert response.content == PNG_BYTES[:4]
    assert response.headers["cache-control"] == "public, max-age=86400"


async def test_an_asset_on_an_unpublished_post_stays_private(
    asset_env: AssetEnv, engine: AsyncEngine
) -> None:
    asset = await add_asset(engine, asset_env.storage, asset_env.user_id)
    await add_post(
        engine,
        author_id=asset_env.user_id,
        asset_id=asset.id,
        published=False,
    )

    async with asgi_client(create_app(asset_env.settings)) as anonymous:
        response = await anonymous.get(f"/v1/assets/{asset.id}")

    assert_problem(response, 404, "not_found")
    assert (await asset_env.http.get(f"/v1/assets/{asset.id}")).status_code == 200

    async with asgi_client(create_app(asset_env.settings)) as other:
        signup = await post_signup(other, f"stranger-{uuid4().hex}@example.com")
        assert signup.status_code == 201, signup.text
        response = await other.get(f"/v1/assets/{asset.id}")

    assert_problem(response, 404, "not_found")


async def test_the_asset_back_reference_also_publishes_membership(
    asset_env: AssetEnv, engine: AsyncEngine
) -> None:
    asset = await add_asset(engine, asset_env.storage, asset_env.user_id)
    post = await add_post(engine, author_id=asset_env.user_id, asset_id=None)

    async with session_scope(create_session_factory(engine)) as session:
        row = await session.get(Asset, asset.id)
        assert row is not None
        row.post_id = post.id

    async with asgi_client(create_app(asset_env.settings)) as anonymous:
        response = await anonymous.get(f"/v1/assets/{asset.id}")

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "public, max-age=86400"


async def test_a_draft_back_reference_is_not_public(
    asset_env: AssetEnv, engine: AsyncEngine
) -> None:
    asset = await add_asset(engine, asset_env.storage, asset_env.user_id)
    post = await add_post(
        engine,
        author_id=asset_env.user_id,
        asset_id=None,
        published=False,
    )

    async with session_scope(create_session_factory(engine)) as session:
        row = await session.get(Asset, asset.id)
        assert row is not None
        row.post_id = post.id

    async with asgi_client(create_app(asset_env.settings)) as anonymous:
        response = await anonymous.get(f"/v1/assets/{asset.id}")

    assert_problem(response, 404, "not_found")


async def test_unpublishing_a_post_hides_the_asset_again(
    asset_env: AssetEnv, engine: AsyncEngine
) -> None:
    asset = await add_asset(engine, asset_env.storage, asset_env.user_id)
    post = await add_post(engine, author_id=asset_env.user_id, asset_id=asset.id)

    async with asgi_client(create_app(asset_env.settings)) as anonymous:
        assert (await anonymous.get(f"/v1/assets/{asset.id}")).status_code == 200

        async with session_scope(create_session_factory(engine)) as session:
            row = await session.get(Post, post.id)
            assert row is not None
            row.published = False

        response = await anonymous.get(f"/v1/assets/{asset.id}")

    assert_problem(response, 404, "not_found")
