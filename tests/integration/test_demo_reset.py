"""Integration tests for `POST /v1/demo/reset`.

The reset endpoint is the one destructive user action, so the tests check both
halves of its contract: everything the caller created is gone (rows and bytes on
disk), and nothing anyone else created or published is touched. Credits are
asserted through the ledger as well as the summary, because "restored exactly
once" is a ledger property.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.db.models import (
    Asset,
    Chat,
    CreditLedgerEntry,
    Generation,
    IdempotencyKey,
    Message,
    Post,
    PostLike,
)
from app.db.seed import IMAGE_MODEL_ID, upsert_models
from app.db.session import create_session_factory, session_scope
from app.domain import assets as asset_service
from app.domain import generation_worker
from app.main import create_app
from app.providers.fake_generation import TINY_PNG, FakeGenerationProvider
from app.storage.local import LocalStorage
from tests.support import TEST_ORIGIN, asgi_client, make_settings, post_signup

STALE_AFTER = timedelta(seconds=300)
FREE_ALLOWANCE = 50


@dataclass
class DemoEnv:
    http: httpx.AsyncClient
    other: httpx.AsyncClient
    app: Any
    factory: async_sessionmaker[AsyncSession]
    storage: LocalStorage
    settings: Settings
    user_id: UUID
    other_id: UUID
    post_id: UUID


@pytest.fixture
async def demo_env(
    migrated_database: str,
    engine: AsyncEngine,
    tmp_path: Path,
) -> AsyncIterator[DemoEnv]:
    settings = make_settings(storage_dir=tmp_path / "storage")
    factory = create_session_factory(engine)
    async with session_scope(factory) as session:
        await upsert_models(session, settings)
        # `process_one` claims globally by design, so clear the queue a previous
        # test may have left before this one seeds its own Generation. Assets are
        # left alone: seeded Explore posts own some of them (`posts.asset_id` is
        # RESTRICT), and they do not affect claiming.
        await session.execute(delete(Generation))
        post = Post(
            author_id=None,
            modality="image",
            title="Published post",
            prompt="A published post",
            model_id=IMAGE_MODEL_ID,
            params={},
            published=True,
        )
        session.add(post)
        await session.flush()
        post_id = post.id

    app = create_app(settings)
    async with asgi_client(app) as http:
        other = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"origin": TEST_ORIGIN},
        )
        try:
            signup = await post_signup(http, f"demo-a-{uuid4().hex}@example.com")
            assert signup.status_code == 201, signup.text
            other_signup = await post_signup(other, f"demo-b-{uuid4().hex}@example.com")
            assert other_signup.status_code == 201, other_signup.text
            yield DemoEnv(
                http=http,
                other=other,
                app=app,
                factory=factory,
                storage=LocalStorage(settings.storage_dir),
                settings=settings,
                user_id=UUID(signup.json()["user"]["id"]),
                other_id=UUID(other_signup.json()["user"]["id"]),
                post_id=post_id,
            )
        finally:
            await other.aclose()


async def seed_chat(env: DemoEnv, user_id: UUID) -> UUID:
    """One chat with one persisted turn, directly as the chat route would."""
    async with session_scope(env.factory) as session:
        chat = Chat(user_id=user_id, title="My chat", model_id=env.settings.ai_chat_model)
        session.add(chat)
        await session.flush()
        session.add(
            Message(
                chat_id=chat.id,
                user_id=user_id,
                role="user",
                content="Hello",
                status="complete",
            )
        )
        return chat.id


async def create_generation(env: DemoEnv) -> httpx.Response:
    return await env.http.post(
        "/v1/generations",
        json={
            "modality": "image",
            "modelId": IMAGE_MODEL_ID,
            "prompt": "A sunlit reading room",
            "params": {"aspectRatio": "1:1", "count": 2, "style": "None"},
        },
        headers={"Idempotency-Key": f"key-{uuid4().hex}"},
    )


async def complete_latest_generation(env: DemoEnv) -> None:
    await generation_worker.process_one(
        env.factory,
        env.storage,
        FakeGenerationProvider(),
        worker_id="demo-reset-test",
        stale_after=STALE_AFTER,
    )


async def like(env: DemoEnv, client: httpx.AsyncClient) -> None:
    response = await client.post(f"/v1/explore/posts/{env.post_id}/like")
    assert response.status_code in {200, 204}, response.text


async def count(env: DemoEnv, model: Any, user_id: UUID) -> int:
    async with session_scope(env.factory) as session:
        return (
            await session.scalar(
                select(func.count()).select_from(model).where(model.user_id == user_id)
            )
            or 0
        )


async def asset_keys(env: DemoEnv, user_id: UUID) -> list[str]:
    async with session_scope(env.factory) as session:
        rows = await session.scalars(select(Asset.storage_key).where(Asset.user_id == user_id))
        return list(rows)


async def ledger_entries(env: DemoEnv, user_id: UUID) -> list[CreditLedgerEntry]:
    async with session_scope(env.factory) as session:
        rows = await session.scalars(
            select(CreditLedgerEntry)
            .where(CreditLedgerEntry.user_id == user_id)
            .order_by(CreditLedgerEntry.created_at, CreditLedgerEntry.id)
        )
        return list(rows)


async def credits(env: DemoEnv, client: httpx.AsyncClient) -> dict[str, Any]:
    response = await client.get("/v1/credits")
    assert response.status_code == 200, response.text
    return response.json()


# --- The full reset ----------------------------------------------------------


async def test_reset_clears_only_the_caller_and_restores_the_allowance(
    demo_env: DemoEnv,
) -> None:
    env = demo_env
    await seed_chat(env, env.user_id)
    assert (await create_generation(env)).status_code == 202
    await complete_latest_generation(env)
    await like(env, env.http)

    # The second account keeps its own workspace.
    await seed_chat(env, env.other_id)
    assert (
        await env.other.post(
            "/v1/generations",
            json={
                "modality": "image",
                "modelId": IMAGE_MODEL_ID,
                "prompt": "A second workspace",
                "params": {"aspectRatio": "1:1", "count": 1, "style": "None"},
            },
            headers={"Idempotency-Key": f"key-{uuid4().hex}"},
        )
    ).status_code == 202
    await like(env, env.other)
    other_asset = await asset_service.create_asset(
        env.factory,
        env.storage,
        user_id=env.other_id,
        kind="image",
        mime_type="image/png",
        data=TINY_PNG,
    )

    caller_keys = await asset_keys(env, env.user_id)
    other_keys = await asset_keys(env, env.other_id)
    assert len(caller_keys) == 2
    assert len(other_keys) == 1
    for key in (*caller_keys, *other_keys):
        assert env.storage.path_for(key).exists()

    response = await env.http.post("/v1/demo/reset")

    assert response.status_code == 204, response.text
    assert response.content == b""

    # The caller's work is gone: chats and their turns, the finished generation
    # and its files, the like, and the create-idempotency record.
    assert await count(env, Chat, env.user_id) == 0
    assert await count(env, Message, env.user_id) == 0
    assert await count(env, Generation, env.user_id) == 0
    assert await count(env, Asset, env.user_id) == 0
    assert await count(env, PostLike, env.user_id) == 0
    assert await count(env, IdempotencyKey, env.user_id) == 0
    for key in caller_keys:
        assert not env.storage.path_for(key).exists()

    # Credits are restored, and only through one reset entry.
    summary = await credits(env, env.http)
    assert summary["balance"] == FREE_ALLOWANCE
    assert summary["held"] == 0
    assert summary["available"] == FREE_ALLOWANCE
    entries = await ledger_entries(env, env.user_id)
    reset_entries = [entry for entry in entries if entry.kind == "reset"]
    assert len(reset_entries) == 1
    assert reset_entries[0].delta_balance == 10
    assert reset_entries[0].delta_held == 0
    assert reset_entries[0].ref_type == "demo_reset"
    assert reset_entries[0].ref_id is None

    # The published post survives; the other account's like is the only one left.
    post_response = await env.http.get(f"/v1/explore/posts/{env.post_id}")
    assert post_response.status_code == 200, post_response.text
    assert post_response.json()["likeCount"] == 1
    assert post_response.json()["likedByMe"] is False

    # The other account is untouched, including its in-flight hold and file.
    assert await count(env, Chat, env.other_id) == 1
    assert await count(env, Generation, env.other_id) == 1
    assert await count(env, Asset, env.other_id) == 1
    assert await count(env, PostLike, env.other_id) == 1
    assert await count(env, IdempotencyKey, env.other_id) == 1
    assert env.storage.path_for(other_asset.storage_key).exists()
    other_summary = await credits(env, env.other)
    assert other_summary["balance"] == FREE_ALLOWANCE
    assert other_summary["held"] == 5


async def test_reset_is_idempotent_and_writes_one_reset_entry(
    demo_env: DemoEnv,
) -> None:
    env = demo_env
    assert (await create_generation(env)).status_code == 202
    assert (await credits(env, env.http))["held"] == 10
    first = await ledger_entries(env, env.user_id)

    assert (await env.http.post("/v1/demo/reset")).status_code == 204
    after_first = await ledger_entries(env, env.user_id)
    assert (await env.http.post("/v1/demo/reset")).status_code == 204
    after_second = await ledger_entries(env, env.user_id)

    assert [entry.kind for entry in after_first] == [entry.kind for entry in first] + ["reset"]
    assert [entry.kind for entry in after_second] == [entry.kind for entry in after_first]
    summary = await credits(env, env.http)
    assert (summary["balance"], summary["held"]) == (FREE_ALLOWANCE, 0)


async def test_reset_refunds_an_in_flight_generation_hold(demo_env: DemoEnv) -> None:
    env = demo_env
    assert (await create_generation(env)).status_code == 202
    assert (await credits(env, env.http))["held"] == 10

    assert (await env.http.post("/v1/demo/reset")).status_code == 204

    summary = await credits(env, env.http)
    assert (summary["balance"], summary["held"], summary["available"]) == (
        FREE_ALLOWANCE,
        0,
        FREE_ALLOWANCE,
    )
    assert await count(env, Generation, env.user_id) == 0
    reset = (await ledger_entries(env, env.user_id))[-1]
    assert reset.kind == "reset"
    assert (reset.delta_balance, reset.delta_held) == (0, -10)


async def test_reset_restores_the_current_plan_allowance(demo_env: DemoEnv) -> None:
    env = demo_env
    plan = await env.http.post("/v1/account/plan", json={"planId": "pro"})
    assert plan.status_code == 200, plan.text
    assert (await credits(env, env.http))["allowance"] == 1000

    assert (await create_generation(env)).status_code == 202
    await complete_latest_generation(env)
    spent = await credits(env, env.http)
    assert (spent["balance"], spent["held"]) == (990, 0)

    assert (await env.http.post("/v1/demo/reset")).status_code == 204

    restored = await credits(env, env.http)
    assert (restored["balance"], restored["held"], restored["allowance"]) == (1000, 0, 1000)
    reset = (await ledger_entries(env, env.user_id))[-1]
    assert reset.kind == "reset"
    assert reset.delta_balance == 10


async def test_reset_requires_authentication(migrated_database: str) -> None:
    settings = make_settings()
    async with asgi_client(create_app(settings)) as anonymous:
        response = await anonymous.post("/v1/demo/reset")

    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"
