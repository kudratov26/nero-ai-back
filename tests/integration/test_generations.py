"""Integration tests for the Generation API contract.

The suite covers the server-owned price, the Credit reservation, modality
validation, idempotent create, caller-scoped reads, and cancel refunding. Each
test gets a fresh app and account, so the in-process rate limiter never leaks
between cases.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.db.models import Asset, CreditLedgerEntry, Generation, IdempotencyKey
from app.db.seed import (
    IMAGE_CREDITS,
    IMAGE_MODEL_ID,
    VIDEO_CREDITS,
    VIDEO_CREDITS_10S,
    VIDEO_MODEL_ID,
    upsert_models,
)
from app.db.session import create_session_factory, session_scope
from app.main import create_app
from tests.support import asgi_client, assert_problem, make_settings, post_signup

IMAGE_BODY: dict[str, Any] = {
    "modality": "image",
    "modelId": IMAGE_MODEL_ID,
    "prompt": "  A sunlit reading room  ",
    "params": {"aspectRatio": "4:5", "count": 2, "style": "Cinematic"},
}
VIDEO_BODY: dict[str, Any] = {
    "modality": "video",
    "modelId": VIDEO_MODEL_ID,
    "prompt": "A slow push through a rain-soaked street",
    "params": {"aspectRatio": "16:9", "duration": 5},
}
AUTO_KEY = "__auto__"


@dataclass
class GenerationEnv:
    http: httpx.AsyncClient
    app: Any
    factory: async_sessionmaker[AsyncSession]
    settings: Settings
    user_id: UUID


@pytest.fixture
async def generation_env(
    migrated_database: str,
    engine: AsyncEngine,
) -> AsyncIterator[GenerationEnv]:
    settings = make_settings()
    factory = create_session_factory(engine)
    async with session_scope(factory) as session:
        await upsert_models(session, settings)

    app = create_app(settings)
    async with asgi_client(app) as http:
        signup = await post_signup(http, f"generation-{uuid4().hex}@example.com")
        assert signup.status_code == 201, signup.text
        yield GenerationEnv(
            http=http,
            app=app,
            factory=factory,
            settings=settings,
            user_id=UUID(signup.json()["user"]["id"]),
        )


async def create(
    env: GenerationEnv,
    *,
    body: dict[str, Any] | None = None,
    key: str | None = AUTO_KEY,
    **overrides: Any,
) -> httpx.Response:
    payload = dict(body or IMAGE_BODY)
    payload.update(overrides)
    headers: dict[str, str] = {}
    if key == AUTO_KEY:
        headers["Idempotency-Key"] = f"key-{uuid4().hex}"
    elif key is not None:
        headers["Idempotency-Key"] = key
    return await env.http.post("/v1/generations", json=payload, headers=headers)


async def credit_summary(env: GenerationEnv) -> dict[str, Any]:
    response = await env.http.get("/v1/credits")
    assert response.status_code == 200, response.text
    return response.json()


async def generations_for(
    env: GenerationEnv,
    *,
    user_id: UUID | None = None,
) -> list[Generation]:
    async with session_scope(env.factory) as session:
        rows = await session.scalars(
            select(Generation)
            .where(Generation.user_id == (user_id or env.user_id))
            .order_by(Generation.created_at, Generation.id)
        )
        return list(rows)


async def generation_row(env: GenerationEnv, generation_id: str) -> Generation:
    async with session_scope(env.factory) as session:
        row = await session.get(Generation, UUID(generation_id))
        assert row is not None
        return row


async def ledger_for(env: GenerationEnv, generation_id: str) -> list[CreditLedgerEntry]:
    async with session_scope(env.factory) as session:
        rows = await session.scalars(
            select(CreditLedgerEntry)
            .where(
                CreditLedgerEntry.user_id == env.user_id,
                CreditLedgerEntry.ref_type == "generation",
                CreditLedgerEntry.ref_id == UUID(generation_id),
            )
            .order_by(CreditLedgerEntry.created_at, CreditLedgerEntry.id)
        )
        return list(rows)


async def idempotency_keys_for(env: GenerationEnv) -> int:
    async with session_scope(env.factory) as session:
        return (
            await session.scalar(
                select(func.count())
                .select_from(IdempotencyKey)
                .where(IdempotencyKey.user_id == env.user_id)
            )
            or 0
        )


async def second_account(env: GenerationEnv) -> tuple[httpx.AsyncClient, UUID]:
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env.app),
        base_url="http://test",
        headers={"origin": "http://localhost:3000"},
    )
    signup = await post_signup(client, f"generation-other-{uuid4().hex}@example.com")
    assert signup.status_code == 201, signup.text
    return client, UUID(signup.json()["user"]["id"])


# --- Create -----------------------------------------------------------------


async def test_create_returns_202_and_reserves_the_server_price(
    generation_env: GenerationEnv,
) -> None:
    response = await create(generation_env)

    assert response.status_code == 202, response.text
    body = response.json()
    assert UUID(body["id"])
    assert body["modality"] == "image"
    assert body["modelId"] == IMAGE_MODEL_ID
    assert body["prompt"] == "A sunlit reading room"
    assert body["params"] == {"aspectRatio": "4:5", "count": 2, "style": "Cinematic"}
    assert body["status"] == "queued"
    assert body["progress"] == 0
    assert body["cost"] == IMAGE_CREDITS * 2
    assert body["provider"] == "Demo"
    assert body["errorCode"] is None
    assert body["assets"] == []
    assert body["createdAt"].endswith("Z")
    assert body["startedAt"] is None
    assert body["finishedAt"] is None

    summary = await credit_summary(generation_env)
    assert (summary["balance"], summary["held"], summary["available"]) == (
        50,
        IMAGE_CREDITS * 2,
        50 - IMAGE_CREDITS * 2,
    )

    row = await generation_row(generation_env, body["id"])
    assert row.cost == IMAGE_CREDITS * 2
    assert row.status == "queued"

    entries = await ledger_for(generation_env, body["id"])
    assert [(entry.kind, entry.delta_balance, entry.delta_held) for entry in entries] == [
        ("reserve", 0, IMAGE_CREDITS * 2)
    ]
    assert entries[0].reason == "Image generation"
    assert entries[0].balance_after == 50
    assert entries[0].held_after == IMAGE_CREDITS * 2


async def test_video_cost_uses_the_duration_price(generation_env: GenerationEnv) -> None:
    short = await create(generation_env, body=VIDEO_BODY)
    assert short.status_code == 202, short.text
    assert short.json()["cost"] == VIDEO_CREDITS

    long = await create(
        generation_env,
        body=VIDEO_BODY,
        params={"aspectRatio": "9:16", "duration": 10},
    )
    assert long.status_code == 202, long.text
    assert long.json()["cost"] == VIDEO_CREDITS_10S

    summary = await credit_summary(generation_env)
    assert summary["held"] == VIDEO_CREDITS + VIDEO_CREDITS_10S


async def test_insufficient_credits_leave_no_generation_and_no_key(
    generation_env: GenerationEnv,
) -> None:
    first = await create(generation_env, body=VIDEO_BODY, params={"duration": 10})
    assert first.status_code == 202, first.text
    key = f"second-{uuid4().hex}"

    blocked = await create(
        generation_env,
        key=key,
        body=VIDEO_BODY,
        params={"duration": 10},
    )

    assert_problem(blocked, 409, "insufficient_credits")
    assert len(await generations_for(generation_env)) == 1
    assert await idempotency_keys_for(generation_env) == 1
    summary = await credit_summary(generation_env)
    assert (summary["balance"], summary["held"], summary["available"]) == (
        50,
        VIDEO_CREDITS_10S,
        50 - VIDEO_CREDITS_10S,
    )


# --- Validation -------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "status", "code"),
    [
        ({"params": {"aspectRatio": "3:2"}}, 422, "invalid_request"),
        ({"params": {"count": 0}}, 422, "invalid_request"),
        ({"params": {"count": 5}}, 422, "invalid_request"),
        ({"params": {"count": "2"}}, 422, "invalid_request"),
        ({"params": {"style": "Vaporwave"}}, 422, "invalid_request"),
        ({"params": {"aspectRatio": "4:5", "cost": 0}}, 422, "invalid_request"),
        ({"params": {"duration": 5}}, 422, "invalid_request"),
        (
            {
                "modality": "video",
                "modelId": VIDEO_MODEL_ID,
                "params": {"duration": 7},
            },
            422,
            "invalid_request",
        ),
        (
            {"modality": "video", "modelId": IMAGE_MODEL_ID, "params": {"duration": 5}},
            422,
            "model_unavailable",
        ),
        ({"modelId": "not-a-model"}, 422, "model_unavailable"),
        ({"cost": 0}, 400, "invalid_request"),
        ({"prompt": ""}, 400, "invalid_request"),
        ({"prompt": "   "}, 400, "invalid_request"),
        ({"prompt": "x" * 4001}, 400, "invalid_request"),
    ],
)
async def test_invalid_requests_are_rejected_without_side_effects(
    generation_env: GenerationEnv,
    overrides: dict[str, Any],
    status: int,
    code: str,
) -> None:
    response = await create(generation_env, **overrides)

    assert_problem(response, status, code)
    assert await generations_for(generation_env) == []
    assert await idempotency_keys_for(generation_env) == 0
    summary = await credit_summary(generation_env)
    assert summary["held"] == 0


async def test_the_prompt_is_trimmed_to_the_documented_limit(
    generation_env: GenerationEnv,
) -> None:
    response = await create(generation_env, prompt=f"  {'x' * 4000}  ")

    assert response.status_code == 202, response.text
    assert len(response.json()["prompt"]) == 4000


# --- Idempotency ------------------------------------------------------------


async def test_replaying_the_same_key_and_body_returns_the_same_generation(
    generation_env: GenerationEnv,
) -> None:
    key = f"replay-{uuid4().hex}"

    first = await create(generation_env, key=key)
    second = await create(generation_env, key=key)

    assert first.status_code == 202, first.text
    assert second.status_code == 202, second.text
    assert second.json()["id"] == first.json()["id"]
    assert len(await generations_for(generation_env)) == 1
    assert await idempotency_keys_for(generation_env) == 1
    summary = await credit_summary(generation_env)
    assert summary["held"] == IMAGE_CREDITS * 2


async def test_the_same_key_with_a_different_body_is_a_conflict(
    generation_env: GenerationEnv,
) -> None:
    key = f"conflict-{uuid4().hex}"
    first = await create(generation_env, key=key)

    conflict = await create(generation_env, key=key, prompt="A different room")

    assert_problem(conflict, 409, "idempotency_conflict")
    assert len(await generations_for(generation_env)) == 1
    summary = await credit_summary(generation_env)
    assert summary["held"] == IMAGE_CREDITS * 2
    assert (await generation_row(generation_env, first.json()["id"])).prompt == (
        "A sunlit reading room"
    )


@pytest.mark.parametrize("key", [None, "", "   ", "k" * 256])
async def test_a_missing_or_malformed_key_is_rejected(
    generation_env: GenerationEnv,
    key: str | None,
) -> None:
    response = await create(generation_env, key=key)

    assert_problem(response, 400, "invalid_request")
    assert await generations_for(generation_env) == []


# --- List, detail, and ownership -------------------------------------------


async def test_listing_is_newest_first_with_an_opaque_cursor(
    generation_env: GenerationEnv,
) -> None:
    created = []
    for count in (1, 2, 3):
        response = await create(generation_env, params={"count": count})
        assert response.status_code == 202, response.text
        created.append(response.json()["id"])

    first = await generation_env.http.get("/v1/generations", params={"limit": 2})
    assert first.status_code == 200, first.text
    page = first.json()
    assert [item["id"] for item in page["items"]] == list(reversed(created))[:2]
    assert page["nextCursor"]

    second = await generation_env.http.get(
        "/v1/generations",
        params={"limit": 2, "cursor": page["nextCursor"]},
    )
    assert second.status_code == 200, second.text
    assert [item["id"] for item in second.json()["items"]] == [created[0]]
    assert second.json()["nextCursor"] is None


async def test_listing_is_scoped_to_the_caller(generation_env: GenerationEnv) -> None:
    mine = await create(generation_env)
    other, other_id = await second_account(generation_env)
    try:
        theirs = await create(
            GenerationEnv(
                http=other,
                app=generation_env.app,
                factory=generation_env.factory,
                settings=generation_env.settings,
                user_id=other_id,
            )
        )
        assert theirs.status_code == 202, theirs.text

        listed = await generation_env.http.get("/v1/generations")

        ids = {item["id"] for item in listed.json()["items"]}
        assert ids == {mine.json()["id"]}
    finally:
        await other.aclose()


async def test_status_filters_before_pagination(generation_env: GenerationEnv) -> None:
    queued = await create(generation_env)
    cancelled = await create(generation_env)
    assert (
        await generation_env.http.post(f"/v1/generations/{cancelled.json()['id']}/cancel")
    ).status_code == 202

    queued_page = await generation_env.http.get("/v1/generations", params={"status": "queued"})
    cancelled_page = await generation_env.http.get(
        "/v1/generations", params={"status": "cancelled"}
    )

    assert [item["id"] for item in queued_page.json()["items"]] == [queued.json()["id"]]
    assert [item["id"] for item in cancelled_page.json()["items"]] == [cancelled.json()["id"]]

    invalid = await generation_env.http.get("/v1/generations", params={"status": "pending"})
    assert_problem(invalid, 400, "invalid_request")


@pytest.mark.parametrize(
    "params",
    [
        {"cursor": "not-a-cursor"},
        {"limit": 0},
        {"limit": 101},
    ],
)
async def test_invalid_pagination_is_rejected(
    generation_env: GenerationEnv,
    params: dict[str, Any],
) -> None:
    response = await generation_env.http.get("/v1/generations", params=params)

    assert_problem(response, 400, "invalid_request")


async def test_detail_is_owned_and_missing_ids_are_404(
    generation_env: GenerationEnv,
) -> None:
    created = await create(generation_env)
    generation_id = created.json()["id"]

    own = await generation_env.http.get(f"/v1/generations/{generation_id}")
    assert own.status_code == 200, own.text
    assert own.json() == created.json()

    missing = await generation_env.http.get("/v1/generations/0193a0e0-0000-7000-8000-000000000000")
    assert_problem(missing, 404, "not_found")

    other, _ = await second_account(generation_env)
    try:
        foreign = await other.get(f"/v1/generations/{generation_id}")
        assert_problem(foreign, 404, "not_found")
        foreign_cancel = await other.post(f"/v1/generations/{generation_id}/cancel")
        assert_problem(foreign_cancel, 404, "not_found")
    finally:
        await other.aclose()

    assert (await generation_row(generation_env, generation_id)).status == "queued"


# --- Cancel -----------------------------------------------------------------


async def test_cancelling_refunds_exactly_once(generation_env: GenerationEnv) -> None:
    created = await create(generation_env)
    generation_id = created.json()["id"]

    first = await generation_env.http.post(f"/v1/generations/{generation_id}/cancel")
    assert first.status_code == 202, first.text
    assert first.json()["status"] == "cancelled"
    assert first.json()["progress"] == 0
    assert first.json()["finishedAt"].endswith("Z")

    second = await generation_env.http.post(f"/v1/generations/{generation_id}/cancel")
    assert second.status_code == 202, second.text
    assert second.json()["status"] == "cancelled"

    summary = await credit_summary(generation_env)
    assert (summary["balance"], summary["held"], summary["available"]) == (50, 0, 50)
    entries = await ledger_for(generation_env, generation_id)
    assert [entry.kind for entry in entries] == ["reserve", "refund"]
    assert [entry.delta_held for entry in entries] == [
        IMAGE_CREDITS * 2,
        -(IMAGE_CREDITS * 2),
    ]


async def test_cancelling_a_running_generation_refunds_it(
    generation_env: GenerationEnv,
) -> None:
    created = await create(generation_env)
    generation_id = created.json()["id"]
    async with session_scope(generation_env.factory) as session:
        row = await session.get(Generation, UUID(generation_id))
        assert row is not None
        row.status = "running"
        row.started_at = row.created_at

    response = await generation_env.http.post(f"/v1/generations/{generation_id}/cancel")

    assert response.status_code == 202, response.text
    assert response.json()["status"] == "cancelled"
    summary = await credit_summary(generation_env)
    assert summary["held"] == 0


@pytest.mark.parametrize("status", ["succeeded", "failed"])
async def test_a_finished_generation_is_not_cancellable(
    generation_env: GenerationEnv,
    status: str,
) -> None:
    created = await create(generation_env)
    generation_id = created.json()["id"]
    async with session_scope(generation_env.factory) as session:
        row = await session.get(Generation, UUID(generation_id))
        assert row is not None
        row.status = status

    response = await generation_env.http.post(f"/v1/generations/{generation_id}/cancel")

    assert_problem(response, 409, "generation_not_cancellable")
    assert (await generation_row(generation_env, generation_id)).status == status
    summary = await credit_summary(generation_env)
    assert summary["held"] == IMAGE_CREDITS * 2


# --- Outputs ----------------------------------------------------------------


async def test_persisted_assets_are_returned_with_the_generation(
    generation_env: GenerationEnv,
) -> None:
    created = await create(generation_env)
    generation_id = UUID(created.json()["id"])
    async with session_scope(generation_env.factory) as session:
        asset = Asset(
            user_id=generation_env.user_id,
            generation_id=generation_id,
            kind="image",
            storage_key=f"assets/{generation_env.user_id}/{uuid4()}.png",
            mime_type="image/png",
            size_bytes=128,
            width=800,
            height=1000,
        )
        session.add(asset)
        await session.flush()
        asset_id = asset.id

    detail = await generation_env.http.get(f"/v1/generations/{generation_id}")

    assert detail.status_code == 200, detail.text
    assert detail.json()["assets"] == [
        {
            "id": str(asset_id),
            "url": f"/v1/assets/{asset_id}",
            "mimeType": "image/png",
            "width": 800,
            "height": 1000,
            "durationMs": None,
        }
    ]


# --- Rate limit -------------------------------------------------------------


async def test_the_generation_rate_limit_is_enforced(
    generation_env: GenerationEnv,
) -> None:
    limiter = generation_env.app.state.rate_limiter
    key = f"generation-create:{generation_env.user_id}"
    for _ in range(10):
        assert limiter.hit(key, limit=10, window_seconds=60) is None

    response = await create(generation_env)

    assert_problem(response, 429, "rate_limited")
    assert response.headers["retry-after"]
    assert await generations_for(generation_env) == []
