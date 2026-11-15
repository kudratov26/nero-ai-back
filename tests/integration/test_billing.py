"""Integration tests for the credit and plan endpoints."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.cursor import encode_cursor
from app.db.models import CreditAccount
from app.db.session import create_session_factory, session_scope
from app.domain import credits
from app.main import create_app
from tests.support import asgi_client, assert_problem, make_settings, post_signup

LEDGER_FIELDS = {
    "id",
    "kind",
    "deltaBalance",
    "deltaHeld",
    "balanceAfter",
    "heldAfter",
    "reason",
    "refType",
    "refId",
    "createdAt",
}


@pytest.fixture
async def account(
    migrated_database: str,
) -> AsyncIterator[tuple[httpx.AsyncClient, UUID]]:
    async with asgi_client(create_app(make_settings())) as http:
        response = await post_signup(http, f"billing-{uuid4().hex}@example.com")
        assert response.status_code == 201, response.text
        yield http, UUID(response.json()["user"]["id"])


async def add_entries(engine: AsyncEngine, user_id: UUID, *, count: int, amount: int = 5) -> None:
    """Append `count` distinct grant entries through the ledger service."""
    async with session_scope(create_session_factory(engine)) as session:
        for index in range(count):
            await credits.grant(session, user_id=user_id, amount=amount, reason=f"grant {index}")


async def hold_credits(engine: AsyncEngine, user_id: UUID, amount: int) -> None:
    async with session_scope(create_session_factory(engine)) as session:
        await credits.reserve(session, user_id=user_id, amount=amount, reason="in flight")


# --- GET /v1/credits ---------------------------------------------------------


async def test_credits_summary_matches_signup(
    account: tuple[httpx.AsyncClient, UUID],
) -> None:
    client, _ = account

    response = await client.get("/v1/credits")

    assert response.status_code == 200
    body = response.json()
    assert body["balance"] == 50
    assert body["held"] == 0
    assert body["available"] == 50
    assert body["allowance"] == 50
    period_start = datetime.fromisoformat(body["periodStart"])
    period_end = datetime.fromisoformat(body["periodEnd"])
    assert (period_end - period_start).days == 30


async def test_credits_summary_tracks_a_hold(
    account: tuple[httpx.AsyncClient, UUID], engine: AsyncEngine
) -> None:
    client, user_id = account
    await hold_credits(engine, user_id, 20)

    body = (await client.get("/v1/credits")).json()

    assert (body["balance"], body["held"], body["available"]) == (50, 20, 30)


async def test_credits_endpoint_requires_auth(
    migrated_database: str,
) -> None:
    async with asgi_client(create_app(make_settings())) as anonymous:
        response = await anonymous.get("/v1/credits")

    assert_problem(response, 401, "unauthorized")


# --- GET /v1/credits/ledger --------------------------------------------------


async def test_ledger_returns_the_opening_grant(
    account: tuple[httpx.AsyncClient, UUID],
) -> None:
    client, _ = account

    response = await client.get("/v1/credits/ledger")

    assert response.status_code == 200
    body = response.json()
    assert body["nextCursor"] is None
    assert len(body["items"]) == 1
    entry = body["items"][0]
    assert set(entry) == LEDGER_FIELDS
    assert entry["kind"] == "grant"
    assert entry["deltaBalance"] == 50
    assert entry["deltaHeld"] == 0
    assert entry["balanceAfter"] == 50
    assert entry["heldAfter"] == 0
    assert entry["reason"] == "Free plan allowance"
    assert entry["refType"] is None
    assert entry["refId"] is None
    assert entry["createdAt"].endswith("Z")
    assert entry["id"] == str(UUID(entry["id"]))


async def test_ledger_is_newest_first(
    account: tuple[httpx.AsyncClient, UUID], engine: AsyncEngine
) -> None:
    client, user_id = account
    await add_entries(engine, user_id, count=3)

    items = (await client.get("/v1/credits/ledger")).json()["items"]

    assert [entry["reason"] for entry in items[:3]] == ["grant 2", "grant 1", "grant 0"]
    assert items[-1]["kind"] == "grant"
    assert items[-1]["reason"] == "Free plan allowance"


async def test_ledger_paginates_with_an_opaque_cursor(
    account: tuple[httpx.AsyncClient, UUID], engine: AsyncEngine
) -> None:
    client, user_id = account
    await add_entries(engine, user_id, count=7)
    expected = 8  # the opening grant plus seven

    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        params = {"limit": 3}
        if cursor is not None:
            params["cursor"] = cursor
        body = (await client.get("/v1/credits/ledger", params=params)).json()
        ids = [entry["id"] for entry in body["items"]]
        assert len(ids) <= 3
        seen.extend(ids)
        pages += 1
        cursor = body["nextCursor"]
        if cursor is None:
            break
        # The cursor stays opaque: it must not expose the sort key.
        assert "created" not in cursor.lower()

    assert pages == 3
    assert len(seen) == expected
    assert len(set(seen)) == expected


@pytest.mark.parametrize(
    "cursor",
    [
        "not-a-cursor",
        encode_cursor({}),
        encode_cursor({"at": "2026-11-15T12:00:00Z"}),
        encode_cursor({"at": "2026-11-15T12:00:00Z", "id": "not-a-uuid"}),
        encode_cursor({"at": "not-a-date", "id": "0193a0e0-0000-7000-8000-000000000000"}),
        # A well-formed but timezone-naive timestamp is still rejected.
        encode_cursor({"at": "2026-11-15T12:00:00", "id": "0193a0e0-0000-7000-8000-000000000000"}),
    ],
)
async def test_ledger_rejects_a_bad_cursor(
    account: tuple[httpx.AsyncClient, UUID], cursor: str
) -> None:
    client, _ = account

    response = await client.get("/v1/credits/ledger", params={"cursor": cursor})

    body = assert_problem(response, 400, "invalid_request")
    assert body["detail"] == "The cursor is not valid."
    assert cursor not in response.text


@pytest.mark.parametrize("limit", [0, 101, -1])
async def test_ledger_bounds_the_limit(account: tuple[httpx.AsyncClient, UUID], limit: int) -> None:
    client, _ = account

    response = await client.get("/v1/credits/ledger", params={"limit": limit})

    assert_problem(response, 400, "invalid_request")


async def test_ledger_is_scoped_to_the_caller(migrated_database: str, engine: AsyncEngine) -> None:
    async with asgi_client(create_app(make_settings())) as first:
        first_response = await post_signup(first, f"ledger-a-{uuid4().hex}@example.com")
        first_id = UUID(first_response.json()["user"]["id"])
    async with asgi_client(create_app(make_settings())) as second:
        await post_signup(second, f"ledger-b-{uuid4().hex}@example.com")
        await add_entries(engine, first_id, count=4)

        items = (await second.get("/v1/credits/ledger")).json()["items"]

    assert [entry["reason"] for entry in items] == ["Free plan allowance"]


# --- POST /v1/account/plan ---------------------------------------------------


async def test_plan_change_refills_and_returns_me(
    account: tuple[httpx.AsyncClient, UUID],
) -> None:
    client, _ = account

    response = await client.post("/v1/account/plan", json={"planId": "pro"})

    assert response.status_code == 200
    body = response.json()
    assert body["plan"] == {"id": "pro", "name": "Pro", "monthlyCredits": 1000}
    assert body["credits"]["balance"] == 1000
    assert body["credits"]["held"] == 0
    assert body["credits"]["available"] == 1000
    assert body["credits"]["allowance"] == 1000
    period_start = datetime.fromisoformat(body["credits"]["periodStart"])
    period_end = datetime.fromisoformat(body["credits"]["periodEnd"])
    assert (period_end - period_start).days == 30

    ledger = (await client.get("/v1/credits/ledger")).json()["items"]
    assert ledger[0]["kind"] == "plan_change"
    assert ledger[0]["deltaBalance"] == 950
    assert ledger[0]["balanceAfter"] == 1000
    assert ledger[0]["refType"] == "plan"


async def test_plan_change_updates_the_account(
    account: tuple[httpx.AsyncClient, UUID], engine: AsyncEngine
) -> None:
    client, user_id = account

    await client.post("/v1/account/plan", json={"planId": "studio"})

    async with session_scope(create_session_factory(engine)) as session:
        stored = await session.get(CreditAccount, user_id)
        assert stored is not None
        assert stored.plan_id == "studio"
        assert stored.allowance == 3000
        assert stored.balance == 3000


async def test_plan_change_to_an_unknown_plan_is_not_found(
    account: tuple[httpx.AsyncClient, UUID],
) -> None:
    client, _ = account

    response = await client.post("/v1/account/plan", json={"planId": "enterprise"})

    assert_problem(response, 404, "not_found")
    assert "enterprise" not in response.text


async def test_plan_change_is_rejected_while_credits_are_held(
    account: tuple[httpx.AsyncClient, UUID], engine: AsyncEngine
) -> None:
    client, user_id = account
    await hold_credits(engine, user_id, 10)

    response = await client.post("/v1/account/plan", json={"planId": "pro"})

    assert_problem(response, 409, "conflict")
    # The plan is unchanged and the hold is intact.
    me = (await client.get("/v1/auth/me")).json()
    assert me["plan"]["id"] == "free"
    assert me["credits"]["held"] == 10


async def test_plan_change_is_disabled_when_configured_off(
    migrated_database: str,
) -> None:
    settings = make_settings(allow_simulated_plan_changes=False)
    async with asgi_client(create_app(settings)) as client:
        signup = await post_signup(client, f"disabled-{uuid4().hex}@example.com")
        assert signup.status_code == 201
        response = await client.post("/v1/account/plan", json={"planId": "pro"})

    assert_problem(response, 404, "not_found")


async def test_plan_change_requires_a_plan_id(
    account: tuple[httpx.AsyncClient, UUID],
) -> None:
    client, _ = account

    response = await client.post("/v1/account/plan", json={})

    assert_problem(response, 400, "invalid_request")
