"""Integration tests for chat CRUD.

Every test drives the HTTP surface, because the invariants that matter here are
contract-level: the response shape, the `404` that hides another account's chat,
the keyset cursor, and the soft delete that keeps rows but hides them.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings
from app.core.cursor import encode_cursor
from app.db.models import Chat
from app.db.models.chat import DEFAULT_TITLE
from app.db.seed import IMAGE_MODEL_ID, upsert_models
from app.db.session import create_session_factory, session_scope
from app.main import create_app
from tests.support import asgi_client, assert_problem, make_settings, post_signup

CHAT_FIELDS = {"id", "title", "modelId", "createdAt", "updatedAt"}
CONFIGURED_CHAT = {
    "ai_base_url": "https://gateway.example.test/v1",
    "ai_api_key": "test-key-not-a-real-secret",
}


async def _seed(engine: AsyncEngine, settings: Settings) -> str:
    """Seed the catalog and return the configured chat model id."""
    async with session_scope(create_session_factory(engine)) as session:
        await upsert_models(session, settings)
    return settings.ai_chat_model


@pytest.fixture
async def chat_api(
    migrated_database: str, engine: AsyncEngine
) -> AsyncIterator[tuple[httpx.AsyncClient, str]]:
    """A signed-in client with the catalog seeded and the chat provider configured."""
    settings = make_settings(**CONFIGURED_CHAT)
    model_id = await _seed(engine, settings)
    async with asgi_client(create_app(settings)) as http:
        signup = await post_signup(http, f"chats-{uuid4().hex}@example.com")
        assert signup.status_code == 201, signup.text
        yield http, model_id


async def create(http: httpx.AsyncClient, model_id: str, **payload: object) -> httpx.Response:
    return await http.post("/v1/chats", json={"modelId": model_id, **payload})


async def walk_pages(http: httpx.AsyncClient, *, limit: int) -> list[dict[str, Any]]:
    """Follow `nextCursor` until it is null, collecting every item in order."""
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(100):  # a bound, so a cursor that never ends fails the test
        params: dict[str, str | int] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        page = (await http.get("/v1/chats", params=params)).json()
        items.extend(page["items"])
        cursor = page["nextCursor"]
        if cursor is None:
            return items
    raise AssertionError("the cursor never terminated")


# --- Create ------------------------------------------------------------------


async def test_create_returns_the_documented_shape(
    chat_api: tuple[httpx.AsyncClient, str],
) -> None:
    http, model_id = chat_api

    response = await create(http, model_id, title="Launch plan")

    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == CHAT_FIELDS
    assert body["title"] == "Launch plan"
    assert body["modelId"] == model_id
    UUID(body["id"])
    assert body["createdAt"].endswith("Z")
    assert body["updatedAt"].endswith("Z")


async def test_create_without_a_title_uses_the_placeholder(
    chat_api: tuple[httpx.AsyncClient, str],
) -> None:
    http, model_id = chat_api

    response = await create(http, model_id)

    assert response.status_code == 201, response.text
    assert response.json()["title"] == DEFAULT_TITLE


async def test_create_persists_the_row_for_the_caller(
    chat_api: tuple[httpx.AsyncClient, str],
    engine: AsyncEngine,
) -> None:
    http, model_id = chat_api
    created = (await create(http, model_id, title="Persisted")).json()

    async with session_scope(create_session_factory(engine)) as session:
        chat = await session.get(Chat, UUID(created["id"]))

    assert chat is not None
    assert chat.title == "Persisted"
    assert chat.deleted_at is None
    assert chat.title_is_custom is True


async def test_an_implicit_title_is_marked_for_derivation(
    chat_api: tuple[httpx.AsyncClient, str],
    engine: AsyncEngine,
) -> None:
    """B3.3 may replace a placeholder title, never one the caller chose."""
    http, model_id = chat_api
    created = (await create(http, model_id)).json()

    async with session_scope(create_session_factory(engine)) as session:
        chat = await session.get(Chat, UUID(created["id"]))

    assert chat is not None
    assert chat.title_is_custom is False


async def test_create_rejects_an_unavailable_model(
    chat_api: tuple[httpx.AsyncClient, str],
) -> None:
    """An image model is a real catalog row, but not a chat model."""
    http, _ = chat_api

    response = await create(http, IMAGE_MODEL_ID)

    body = assert_problem(response, 422, "model_unavailable")
    assert IMAGE_MODEL_ID not in body["detail"]


async def test_create_rejects_an_unknown_model(chat_api: tuple[httpx.AsyncClient, str]) -> None:
    http, _ = chat_api

    response = await create(http, "no-such-model")

    assert_problem(response, 422, "model_unavailable")


async def test_create_rejects_an_unadvertised_chat_model(
    migrated_database: str, engine: AsyncEngine
) -> None:
    """A chat model with no configured provider must not be reachable."""
    settings = make_settings()  # no AI_BASE_URL / AI_API_KEY
    model_id = await _seed(engine, settings)
    async with asgi_client(create_app(settings)) as http:
        await post_signup(http, f"chats-{uuid4().hex}@example.com")

        response = await create(http, model_id)

    assert_problem(response, 422, "model_unavailable")


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"modelId": ""},
        {"modelId": "m" * 121},
        {"modelId": "deepseek-v4.1-flash", "title": "   "},
        {"modelId": "deepseek-v4.1-flash", "title": "t" * 81},
        {"modelId": "deepseek-v4.1-flash", "creditCost": 0},
    ],
)
async def test_create_rejects_an_invalid_body(
    chat_api: tuple[httpx.AsyncClient, str], payload: dict[str, object]
) -> None:
    http, _ = chat_api

    response = await http.post("/v1/chats", json=payload)

    assert_problem(response, 400, "invalid_request")


# --- Read --------------------------------------------------------------------


async def test_get_returns_the_chat(chat_api: tuple[httpx.AsyncClient, str]) -> None:
    http, model_id = chat_api
    created = (await create(http, model_id, title="Readable")).json()

    response = await http.get(f"/v1/chats/{created['id']}")

    assert response.status_code == 200, response.text
    assert response.json() == created


async def test_get_of_an_unknown_chat_is_not_found(
    chat_api: tuple[httpx.AsyncClient, str],
) -> None:
    http, _ = chat_api

    response = await http.get(f"/v1/chats/{uuid4()}")

    assert_problem(response, 404, "not_found")


async def test_get_of_a_non_uuid_is_a_validation_error(
    chat_api: tuple[httpx.AsyncClient, str],
) -> None:
    http, _ = chat_api

    response = await http.get("/v1/chats/not-a-uuid")

    assert_problem(response, 400, "invalid_request")


# --- Update ------------------------------------------------------------------


async def test_rename_updates_the_title_and_the_timestamp(
    chat_api: tuple[httpx.AsyncClient, str],
) -> None:
    http, model_id = chat_api
    created = (await create(http, model_id, title="Before")).json()

    response = await http.patch(f"/v1/chats/{created['id']}", json={"title": "  After  "})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["title"] == "After"
    assert body["createdAt"] == created["createdAt"]
    assert body["updatedAt"] >= created["updatedAt"]


async def test_rename_marks_the_title_as_custom(
    chat_api: tuple[httpx.AsyncClient, str], engine: AsyncEngine
) -> None:
    """A renamed placeholder must not be overwritten by the first message."""
    http, model_id = chat_api
    created = (await create(http, model_id)).json()

    await http.patch(f"/v1/chats/{created['id']}", json={"title": "Mine"})

    async with session_scope(create_session_factory(engine)) as session:
        chat = await session.get(Chat, UUID(created["id"]))
    assert chat is not None
    assert chat.title_is_custom is True


@pytest.mark.parametrize("payload", [{}, {"title": ""}, {"title": "   "}, {"title": "x" * 81}])
async def test_rename_rejects_an_invalid_body(
    chat_api: tuple[httpx.AsyncClient, str], payload: dict[str, object]
) -> None:
    http, model_id = chat_api
    created = (await create(http, model_id)).json()

    response = await http.patch(f"/v1/chats/{created['id']}", json=payload)

    assert_problem(response, 400, "invalid_request")


async def test_rename_of_an_unknown_chat_is_not_found(
    chat_api: tuple[httpx.AsyncClient, str],
) -> None:
    http, _ = chat_api

    response = await http.patch(f"/v1/chats/{uuid4()}", json={"title": "Nope"})

    assert_problem(response, 404, "not_found")


# --- Delete ------------------------------------------------------------------


async def test_delete_hides_the_chat_but_keeps_the_row(
    chat_api: tuple[httpx.AsyncClient, str], engine: AsyncEngine
) -> None:
    http, model_id = chat_api
    created = (await create(http, model_id)).json()

    response = await http.delete(f"/v1/chats/{created['id']}")

    assert response.status_code == 204
    assert response.content == b""
    assert_problem(await http.get(f"/v1/chats/{created['id']}"), 404, "not_found")

    async with session_scope(create_session_factory(engine)) as session:
        chat = await session.get(Chat, UUID(created["id"]))
    assert chat is not None, "the row must survive a soft delete"
    assert chat.deleted_at is not None


async def test_delete_removes_the_chat_from_the_listing(
    chat_api: tuple[httpx.AsyncClient, str],
) -> None:
    http, model_id = chat_api
    kept = (await create(http, model_id, title="Kept")).json()
    dropped = (await create(http, model_id, title="Dropped")).json()

    await http.delete(f"/v1/chats/{dropped['id']}")

    listed = (await http.get("/v1/chats")).json()["items"]
    assert [chat["id"] for chat in listed] == [kept["id"]]


async def test_delete_is_idempotent(chat_api: tuple[httpx.AsyncClient, str]) -> None:
    """A retried delete succeeds; only an unknown or foreign chat is a `404`."""
    http, model_id = chat_api
    created = (await create(http, model_id)).json()

    assert (await http.delete(f"/v1/chats/{created['id']}")).status_code == 204
    assert (await http.delete(f"/v1/chats/{created['id']}")).status_code == 204


async def test_delete_of_an_unknown_chat_is_not_found(
    chat_api: tuple[httpx.AsyncClient, str],
) -> None:
    http, _ = chat_api

    response = await http.delete(f"/v1/chats/{uuid4()}")

    assert_problem(response, 404, "not_found")


async def test_a_deleted_chat_cannot_be_renamed(
    chat_api: tuple[httpx.AsyncClient, str],
) -> None:
    http, model_id = chat_api
    created = (await create(http, model_id)).json()
    await http.delete(f"/v1/chats/{created['id']}")

    response = await http.patch(f"/v1/chats/{created['id']}", json={"title": "Back"})

    assert_problem(response, 404, "not_found")


# --- Listing and pagination --------------------------------------------------


async def test_the_listing_is_newest_first(chat_api: tuple[httpx.AsyncClient, str]) -> None:
    http, model_id = chat_api
    titles = [f"Chat {index}" for index in range(3)]
    for title in titles:
        assert (await create(http, model_id, title=title)).status_code == 201

    listed = (await http.get("/v1/chats")).json()

    assert [chat["title"] for chat in listed["items"]] == list(reversed(titles))
    assert listed["nextCursor"] is None
    assert set(listed["items"][0]) == CHAT_FIELDS


async def test_the_listing_is_scoped_to_the_caller(
    chat_api: tuple[httpx.AsyncClient, str], engine: AsyncEngine, migrated_database: str
) -> None:
    http, model_id = chat_api
    mine = (await create(http, model_id, title="Mine")).json()

    settings = make_settings(**CONFIGURED_CHAT)
    async with asgi_client(create_app(settings)) as other:
        await post_signup(other, f"other-{uuid4().hex}@example.com")
        theirs = (await create(other, model_id, title="Theirs")).json()

        listed = (await other.get("/v1/chats")).json()["items"]

    assert [chat["id"] for chat in listed] == [theirs["id"]]
    assert mine["id"] not in {chat["id"] for chat in listed}


async def test_pagination_walks_every_chat_exactly_once(
    chat_api: tuple[httpx.AsyncClient, str],
) -> None:
    http, model_id = chat_api
    created = []
    for index in range(5):
        created.append((await create(http, model_id, title=f"Chat {index}")).json())

    items = await walk_pages(http, limit=2)

    assert [chat["id"] for chat in items] == [chat["id"] for chat in reversed(created)]


async def test_a_chat_renamed_ahead_of_the_cursor_is_skipped_not_repeated(
    chat_api: tuple[httpx.AsyncClient, str],
) -> None:
    """The documented keyset tradeoff: a row that moves up is not revisited.

    Keyset pagination compares against a fixed sort key, so renaming a chat that
    has already been paged past moves it above the cursor and the next page does
    not contain it. What must never happen is a *repeat*: a client that appends
    pages would then render the same chat twice.
    """
    http, model_id = chat_api
    created = [(await create(http, model_id, title=f"Chat {index}")).json() for index in range(4)]

    first = (await http.get("/v1/chats", params={"limit": 2})).json()
    assert len(first["items"]) == 2
    # This chat sits on the first page; renaming it bumps it above the cursor.
    renamed = first["items"][0]["id"]
    await http.patch(f"/v1/chats/{renamed}", json={"title": "Bumped"})

    second = (
        await http.get("/v1/chats", params={"limit": 2, "cursor": first["nextCursor"]})
    ).json()

    seen = [chat["id"] for chat in first["items"]] + [chat["id"] for chat in second["items"]]
    assert len(seen) == len(set(seen)), "a page must never repeat a chat"
    assert renamed not in [chat["id"] for chat in second["items"]]

    # It is not lost either: the next walk from the start sees it at the top.
    fresh = await walk_pages(http, limit=10)
    assert {chat["id"] for chat in fresh} == {row["id"] for row in created}
    assert next(chat["id"] for chat in fresh) == renamed


async def test_a_bad_cursor_is_rejected(chat_api: tuple[httpx.AsyncClient, str]) -> None:
    http, _ = chat_api

    response = await http.get("/v1/chats", params={"cursor": "not-a-cursor"})

    assert_problem(response, 400, "invalid_request")


async def test_a_cursor_with_the_wrong_shape_is_rejected(
    chat_api: tuple[httpx.AsyncClient, str],
) -> None:
    """A cursor from another endpoint's shape is opaque, not a valid keyset."""
    http, _ = chat_api
    cursor = encode_cursor({"createdAt": "2026-11-15T12:00:00Z", "id": str(uuid4())})

    response = await http.get("/v1/chats", params={"cursor": cursor})

    assert_problem(response, 400, "invalid_request")


@pytest.mark.parametrize("limit", [0, 101, -1])
async def test_an_out_of_range_limit_is_rejected(
    chat_api: tuple[httpx.AsyncClient, str], limit: int
) -> None:
    http, _ = chat_api

    response = await http.get("/v1/chats", params={"limit": limit})

    assert_problem(response, 400, "invalid_request")


# --- Ordering edge cases -----------------------------------------------------


async def test_chats_with_the_same_timestamp_are_ordered_by_id(
    chat_api: tuple[httpx.AsyncClient, str], engine: AsyncEngine
) -> None:
    """The cursor's tiebreaker must be deterministic, or a page repeats a row."""
    http, model_id = chat_api
    created = [(await create(http, model_id, title=f"Tie {index}")).json() for index in range(3)]
    stamp = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)

    async with session_scope(create_session_factory(engine)) as session:
        for row in created:
            chat = await session.get(Chat, UUID(row["id"]))
            assert chat is not None
            chat.updated_at = stamp

    seen = await walk_pages(http, limit=1)

    assert sorted(chat["id"] for chat in seen) == sorted(row["id"] for row in created)
    assert len(seen) == len({chat["id"] for chat in seen}) == 3


async def test_a_chat_beyond_the_last_page_yields_no_cursor(
    chat_api: tuple[httpx.AsyncClient, str],
) -> None:
    """`nextCursor` is null when the page filled exactly, with nothing behind it."""
    http, model_id = chat_api
    for index in range(2):
        await create(http, model_id, title=f"Chat {index}")

    page = (await http.get("/v1/chats", params={"limit": 2})).json()

    assert len(page["items"]) == 2
    assert page["nextCursor"] is None
