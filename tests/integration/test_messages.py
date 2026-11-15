"""Integration tests for message persistence and the SSE reply stream.

The transport under test is the documented event protocol: the test collects the
frames, asserts their order, and then checks the rows and ledger effects the
stream left behind. Credits are asserted through the ledger, because "committed
exactly once" is a ledger property, not just an account balance.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.db.models import Chat, CreditAccount, CreditLedgerEntry, Message, Model
from app.db.seed import upsert_models
from app.db.session import create_session_factory, session_scope
from app.domain import messages as message_service
from app.main import create_app
from app.providers.base import (
    ChatDelta,
    ChatParams,
    ChatUsage,
    ProviderMessage,
    ProviderModel,
    ProviderRateLimitError,
    ProviderTimeoutError,
)
from app.providers.fake import FakeChatProvider
from tests.support import asgi_client, assert_problem, make_settings, post_signup

CONFIGURED_CHAT = {
    "ai_base_url": "https://gateway.example.test/v1",
    "ai_api_key": "test-key-not-a-real-secret",
}
REPLY = "Shoot the campaign from three angles."
CHAT_MESSAGE_CREDITS = 1


@dataclass
class ChatEnv:
    http: httpx.AsyncClient
    app: Any
    engine: AsyncEngine
    factory: async_sessionmaker[AsyncSession]
    fake: FakeChatProvider
    settings: Settings
    model_id: str
    chat_id: str
    user_id: UUID


def parse_sse(body: str) -> list[tuple[str | None, dict[str, Any] | None]]:
    """Parse the response the way a browser's EventSource would.

    A comment-only block is reported as `("comment", None)`, which is how the
    heartbeat is distinguished from a named event.
    """
    frames: list[tuple[str | None, dict[str, Any] | None]] = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        if block.lstrip().startswith(":"):
            frames.append(("comment", None))
            continue
        name: str | None = None
        data: dict[str, Any] | None = None
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data = json.loads(line.split(":", 1)[1].strip())
        frames.append((name, data))
    return frames


def names(frames: Sequence[tuple[str | None, dict[str, Any] | None]]) -> list[str | None]:
    return [name for name, _ in frames]


async def send(
    env: ChatEnv,
    content: str = "Give me three campaign angles.",
    *,
    chat_id: str | None = None,
    **payload: object,
) -> httpx.Response:
    return await env.http.post(
        f"/v1/chats/{chat_id or env.chat_id}/messages",
        json={"content": content, **payload},
        headers={"accept": "text/event-stream"},
    )


async def messages_for(env: ChatEnv, chat_id: str | None = None) -> list[Message]:
    async with session_scope(env.factory) as session:
        rows = await session.scalars(
            select(Message)
            .where(Message.chat_id == UUID(chat_id or env.chat_id))
            .order_by(Message.created_at, Message.id)
        )
        return list(rows)


async def ledger_for(env: ChatEnv, ref_id: UUID) -> list[CreditLedgerEntry]:
    async with session_scope(env.factory) as session:
        rows = await session.scalars(
            select(CreditLedgerEntry)
            .where(
                CreditLedgerEntry.user_id == env.user_id,
                CreditLedgerEntry.ref_type == "chat_message",
                CreditLedgerEntry.ref_id == ref_id,
            )
            .order_by(CreditLedgerEntry.created_at, CreditLedgerEntry.id)
        )
        return list(rows)


async def account(env: ChatEnv) -> CreditAccount:
    async with session_scope(env.factory) as session:
        row = await session.get(CreditAccount, env.user_id)
        assert row is not None
        return row


@pytest.fixture
async def chat_env(migrated_database: str, engine: AsyncEngine) -> AsyncIterator[ChatEnv]:
    settings = make_settings(**CONFIGURED_CHAT)
    factory = create_session_factory(engine)
    async with session_scope(factory) as session:
        await upsert_models(session, settings)
    fake = FakeChatProvider(reply=REPLY, chunk_size=5, usage=ChatUsage(12, 80))
    app = create_app(settings, chat_provider=fake)
    async with asgi_client(app) as http:
        signup = await post_signup(http, f"messages-{uuid4().hex}@example.com")
        assert signup.status_code == 201, signup.text
        created = await http.post("/v1/chats", json={"modelId": settings.ai_chat_model})
        assert created.status_code == 201, created.text
        yield ChatEnv(
            http=http,
            app=app,
            engine=engine,
            factory=factory,
            fake=fake,
            settings=settings,
            model_id=settings.ai_chat_model,
            chat_id=created.json()["id"],
            user_id=UUID(signup.json()["user"]["id"]),
        )


# --- Success path ------------------------------------------------------------


async def test_stream_emits_the_documented_events_in_order(chat_env: ChatEnv) -> None:
    response = await send(chat_env, clientId="c-1")

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache, no-transform"
    assert response.headers["x-accel-buffering"] == "no"

    frames = parse_sse(response.text)
    assert names(frames)[0] == "message.started"
    assert names(frames)[-2:] == ["message.completed", "done"]
    assert set(names(frames)[1:-2]) == {"message.delta"}

    started = frames[0][1]
    assert started is not None
    assert started["userMessageId"] != started["assistantMessageId"]
    assert started["modelId"] == chat_env.model_id

    deltas = [data["delta"] for name, data in frames if name == "message.delta"]
    assert "".join(deltas) == REPLY

    completed = frames[-2][1]
    assert completed is not None
    assert completed["messageId"] == started["assistantMessageId"]
    assert completed["content"] == REPLY
    assert completed["usage"] == {"promptTokens": 12, "completionTokens": 80}
    assert completed["credits"] == {
        "balance": 49,
        "held": 0,
        "available": 49,
        "charged": CHAT_MESSAGE_CREDITS,
    }
    assert frames[-1] == ("done", {})


async def test_the_stream_persists_both_turns_and_commits_once(chat_env: ChatEnv) -> None:
    response = await send(chat_env, clientId="c-1")
    assert response.status_code == 200, response.text
    started = parse_sse(response.text)[0][1]
    assert started is not None
    assistant_id = UUID(started["assistantMessageId"])

    rows = await messages_for(chat_env)
    assert [row.role for row in rows] == ["user", "assistant"]
    user, assistant = rows
    assert user.content == "Give me three campaign angles."
    assert user.status == "complete"
    assert user.client_id == "c-1"
    assert user.model_id is None
    assert assistant.id == assistant_id
    assert assistant.content == REPLY
    assert assistant.status == "complete"
    assert assistant.model_id == chat_env.model_id
    assert (assistant.prompt_tokens, assistant.completion_tokens) == (12, 80)
    assert assistant.error_code is None

    entries = await ledger_for(chat_env, assistant_id)
    assert [entry.kind for entry in entries] == ["reserve", "commit"]
    assert [(entry.delta_balance, entry.delta_held) for entry in entries] == [(0, 1), (-1, -1)]
    assert entries[-1].balance_after == 49

    refreshed = await account(chat_env)
    assert (refreshed.balance, refreshed.held) == (49, 0)


async def test_the_first_message_derives_the_title_and_later_ones_do_not(
    chat_env: ChatEnv,
) -> None:
    assert (await send(chat_env, "First angle")).status_code == 200
    assert (await send(chat_env, "A completely different subject")).status_code == 200

    async with session_scope(chat_env.factory) as session:
        chat = await session.get(Chat, UUID(chat_env.chat_id))

    assert chat is not None
    assert chat.title == "First angle"
    assert chat.title_is_custom is False


async def test_a_renamed_chat_keeps_its_title(chat_env: ChatEnv) -> None:
    renamed = await chat_env.http.patch(
        f"/v1/chats/{chat_env.chat_id}", json={"title": "My campaign"}
    )
    assert renamed.status_code == 200

    assert (await send(chat_env, "First angle")).status_code == 200

    async with session_scope(chat_env.factory) as session:
        chat = await session.get(Chat, UUID(chat_env.chat_id))
    assert chat is not None
    assert chat.title == "My campaign"
    assert chat.title_is_custom is True


async def test_the_provider_receives_history_in_order(chat_env: ChatEnv) -> None:
    assert (await send(chat_env, "First")).status_code == 200
    assert (await send(chat_env, "Second")).status_code == 200

    first, second = chat_env.fake.requests
    assert [(message.role, message.content) for message in first.messages] == [("user", "First")]
    assert [(message.role, message.content) for message in second.messages] == [
        ("user", "First"),
        ("assistant", REPLY),
        ("user", "Second"),
    ]
    assert second.model.slug == chat_env.model_id
    assert second.model.provider_model_id == chat_env.model_id


# --- Provider failures -------------------------------------------------------


async def test_a_provider_timeout_persists_partial_content_and_refunds(
    chat_env: ChatEnv,
) -> None:
    chat_env.fake.fail_with = ProviderTimeoutError("The model did not respond in time.")
    chat_env.fake.fail_after = 2

    response = await send(chat_env)

    frames = parse_sse(response.text)
    # A failure is terminal: no `done` after `message.failed`.
    assert names(frames)[-1] == "message.failed"
    assert "done" not in names(frames)
    failed = frames[-1][1]
    assert failed is not None
    assert failed["code"] == "provider_timeout"
    assert failed["refunded"] is True
    assistant_id = UUID(failed["messageId"])

    rows = await messages_for(chat_env)
    assistant = next(row for row in rows if row.role == "assistant")
    assert assistant.id == assistant_id
    assert assistant.status == "failed"
    assert assistant.error_code == "provider_timeout"
    assert assistant.content == REPLY[:10]
    assert (assistant.prompt_tokens, assistant.completion_tokens) == (None, None)

    entries = await ledger_for(chat_env, assistant_id)
    assert [entry.kind for entry in entries] == ["reserve", "refund"]
    refreshed = await account(chat_env)
    assert (refreshed.balance, refreshed.held) == (50, 0)


async def test_a_provider_rate_limit_is_mapped_and_refunded(chat_env: ChatEnv) -> None:
    chat_env.fake.fail_with = ProviderRateLimitError("The provider is rate limiting requests.")
    chat_env.fake.fail_after = 0

    response = await send(chat_env)

    frames = parse_sse(response.text)
    assert names(frames) == ["message.started", "message.failed"]
    failed = frames[-1][1]
    assert failed is not None
    assert failed["code"] == "rate_limited"
    assert failed["refunded"] is True


# --- Preflight errors --------------------------------------------------------


async def test_a_message_cannot_name_a_different_model(chat_env: ChatEnv) -> None:
    response = await send(chat_env, modelId="some-other-model")

    assert_problem(response, 400, "invalid_request")
    assert await messages_for(chat_env) == []
    assert (await account(chat_env)).held == 0


async def test_a_retired_model_is_rejected_before_any_work(chat_env: ChatEnv) -> None:
    async with session_scope(chat_env.factory) as session:
        model = await session.get(Model, chat_env.model_id)
        assert model is not None
        model.available = False

    response = await send(chat_env)

    assert_problem(response, 422, "model_unavailable")
    assert await messages_for(chat_env) == []
    assert (await account(chat_env)).held == 0


async def test_insufficient_credits_is_a_json_conflict(chat_env: ChatEnv) -> None:
    async with session_scope(chat_env.factory) as session:
        row = await session.get(CreditAccount, chat_env.user_id)
        assert row is not None
        row.balance = 0

    response = await send(chat_env)

    assert_problem(response, 409, "insufficient_credits")
    assert await messages_for(chat_env) == []
    assert (await account(chat_env)).held == 0


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"content": ""},
        {"content": "   "},
        {"content": "x" * 4001},
        {"content": "hi", "clientId": ""},
        {"content": "hi", "clientId": "   "},
        {"content": "hi", "modelId": ""},
        {"content": "hi", "unknown": True},
    ],
)
async def test_an_invalid_body_is_rejected(chat_env: ChatEnv, payload: dict[str, object]) -> None:
    response = await chat_env.http.post(
        f"/v1/chats/{chat_env.chat_id}/messages",
        json=payload,
        headers={"accept": "text/event-stream"},
    )

    assert_problem(response, 400, "invalid_request")


async def test_the_chat_rate_limit_is_enforced(chat_env: ChatEnv) -> None:
    limiter = chat_env.app.state.rate_limiter
    key = f"chat-message:{chat_env.user_id}"
    for _ in range(30):
        assert limiter.hit(key, limit=30, window_seconds=60) is None

    response = await send(chat_env)

    assert_problem(response, 429, "rate_limited")
    assert await messages_for(chat_env) == []


# --- Heartbeats and unbuffered delivery -------------------------------------


async def test_an_idle_provider_produces_a_heartbeat_before_the_first_delta(
    chat_env: ChatEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(message_service, "HEARTBEAT_SECONDS", 0.01)
    chat_env.fake.delay_seconds = 0.05

    response = await send(chat_env)

    frames = parse_sse(response.text)
    first_comment = names(frames).index("comment")
    first_delta = names(frames).index("message.delta")
    assert first_comment < first_delta
    assert frames[first_comment] == ("comment", None)


class _GatedProvider:
    """Yields one delta, then waits until the test opens the gate."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True

    async def stream_chat(
        self,
        *,
        messages: Sequence[ProviderMessage],
        model: ProviderModel,
        params: ChatParams,
    ) -> AsyncIterator[ChatDelta]:
        yield ChatDelta(text="first")
        await self.release.wait()
        yield ChatDelta(text="second", finish_reason="stop", usage=ChatUsage(1, 2))


async def test_deltas_are_yielded_before_the_provider_finishes(chat_env: ChatEnv) -> None:
    """The generator does not accumulate the whole reply before the first event."""
    prepared = await message_service.prepare_message(
        chat_env.factory,
        user_id=chat_env.user_id,
        chat_id=UUID(chat_env.chat_id),
        content="Gate the stream",
        requested_model_id=None,
        client_id=None,
        provider_available=True,
    )
    provider = _GatedProvider()
    stream = message_service.stream_message(
        chat_env.factory,
        provider=provider,
        prepared=prepared,
    )
    try:
        started = await anext(stream)
        assert isinstance(started, message_service.StartedEvent)
        first = await anext(stream)
        assert isinstance(first, message_service.DeltaEvent)
        assert first.delta == "first"

        pending = asyncio.ensure_future(anext(stream))
        await asyncio.sleep(0.01)
        assert not pending.done(), "the second delta must wait for the provider"

        provider.release.set()
        second = await pending
        assert isinstance(second, message_service.DeltaEvent)
        assert second.delta == "second"
        completed = await anext(stream)
        assert isinstance(completed, message_service.CompletedEvent)
        done = await anext(stream)
        assert isinstance(done, message_service.DoneEvent)
    finally:
        await stream.aclose()
    assert provider.closed is False


# --- Persisted history -------------------------------------------------------

MESSAGE_FIELDS = {
    "id",
    "role",
    "content",
    "modelId",
    "status",
    "promptTokens",
    "completionTokens",
    "errorCode",
    "clientId",
    "createdAt",
    "updatedAt",
}


async def test_history_returns_the_documented_shape_newest_first(chat_env: ChatEnv) -> None:
    assert (await send(chat_env, clientId="c-1")).status_code == 200

    response = await chat_env.http.get(f"/v1/chats/{chat_env.chat_id}/messages")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["nextCursor"] is None
    assert [item["role"] for item in body["items"]] == ["assistant", "user"]
    assistant, user = body["items"]
    assert set(assistant) == MESSAGE_FIELDS
    assert assistant["content"] == REPLY
    assert assistant["status"] == "complete"
    assert assistant["modelId"] == chat_env.model_id
    assert user["content"] == "Give me three campaign angles."
    assert user["clientId"] == "c-1"
    assert user["modelId"] is None
    assert assistant["createdAt"].endswith("Z")
    assert UUID(assistant["id"])


async def test_history_pagination_walks_every_message_exactly_once(chat_env: ChatEnv) -> None:
    for index in range(3):
        assert (await send(chat_env, f"Message {index}")).status_code == 200

    collected: list[dict[str, Any]] = []
    before: str | None = None
    for _ in range(10):
        params: dict[str, str | int] = {"limit": 2}
        if before is not None:
            params["before"] = before
        page = (
            await chat_env.http.get(f"/v1/chats/{chat_env.chat_id}/messages", params=params)
        ).json()
        collected.extend(page["items"])
        before = page["nextCursor"]
        if before is None:
            break

    rows = await messages_for(chat_env)
    expected = [str(row.id) for row in reversed(rows)]
    assert [item["id"] for item in collected] == expected
    assert len(collected) == 6
    assert len({item["id"] for item in collected}) == 6


async def test_history_includes_a_failed_partial_reply(chat_env: ChatEnv) -> None:
    chat_env.fake.fail_with = ProviderTimeoutError("The model did not respond in time.")
    chat_env.fake.fail_after = 1
    assert (await send(chat_env)).status_code == 200

    items = (await chat_env.http.get(f"/v1/chats/{chat_env.chat_id}/messages")).json()["items"]

    assistant = next(item for item in items if item["role"] == "assistant")
    assert assistant["status"] == "failed"
    assert assistant["errorCode"] == "provider_timeout"
    assert assistant["content"] == REPLY[:5]


async def test_a_deleted_chat_cannot_be_read_or_streamed(chat_env: ChatEnv) -> None:
    assert (await chat_env.http.delete(f"/v1/chats/{chat_env.chat_id}")).status_code == 204

    history = await chat_env.http.get(f"/v1/chats/{chat_env.chat_id}/messages")
    stream = await send(chat_env)

    assert_problem(history, 404, "not_found")
    assert_problem(stream, 404, "not_found")
    assert await messages_for(chat_env) == []


async def test_history_rejects_a_bad_cursor_and_limits(chat_env: ChatEnv) -> None:
    bad_cursor = await chat_env.http.get(
        f"/v1/chats/{chat_env.chat_id}/messages", params={"before": "not-a-cursor"}
    )
    zero_limit = await chat_env.http.get(
        f"/v1/chats/{chat_env.chat_id}/messages", params={"limit": 0}
    )
    big_limit = await chat_env.http.get(
        f"/v1/chats/{chat_env.chat_id}/messages", params={"limit": 101}
    )

    assert_problem(bad_cursor, 400, "invalid_request")
    assert_problem(zero_limit, 400, "invalid_request")
    assert_problem(big_limit, 400, "invalid_request")


async def test_a_duplicate_client_id_is_a_conflict_before_spending(chat_env: ChatEnv) -> None:
    """The partial unique index is a backstop; the preflight returns a clean 409."""
    assert (await send(chat_env, clientId="same-id")).status_code == 200

    duplicate = await send(chat_env, clientId="same-id")

    assert_problem(duplicate, 409, "conflict")
    rows = await messages_for(chat_env)
    assert [row.role for row in rows] == ["user", "assistant"]
    assert (await account(chat_env)).balance == 49


class _BrokenProvider:
    """An adapter bug, not a `ProviderError`."""

    async def aclose(self) -> None:
        pass

    async def stream_chat(
        self,
        *,
        messages: Sequence[ProviderMessage],
        model: ProviderModel,
        params: ChatParams,
    ) -> AsyncIterator[ChatDelta]:
        raise RuntimeError("adapter bug")
        yield ChatDelta()  # pragma: no cover - makes this an async generator


async def test_an_unexpected_provider_error_refunds_and_persists(
    chat_env: ChatEnv,
) -> None:
    prepared = await message_service.prepare_message(
        chat_env.factory,
        user_id=chat_env.user_id,
        chat_id=UUID(chat_env.chat_id),
        content="Break the adapter",
        requested_model_id=None,
        client_id=None,
        provider_available=True,
    )

    events = [
        event
        async for event in message_service.stream_message(
            chat_env.factory, provider=_BrokenProvider(), prepared=prepared
        )
    ]

    assert isinstance(events[0], message_service.StartedEvent)
    failed = events[-1]
    assert isinstance(failed, message_service.FailedEvent)
    assert failed.code == "provider_error"
    assert failed.detail == message_service.STREAM_FAILED_DETAIL
    assert failed.refunded is True

    assistant = await _assistant_row(chat_env, prepared.assistant_message_id)
    assert assistant.status == "failed"
    assert assistant.error_code == "provider_error"
    entries = await ledger_for(chat_env, prepared.assistant_message_id)
    assert [entry.kind for entry in entries] == ["reserve", "refund"]
    refreshed = await account(chat_env)
    assert (refreshed.balance, refreshed.held) == (50, 0)


async def _assistant_row(env: ChatEnv, message_id: UUID) -> Message:
    async with session_scope(env.factory) as session:
        row = await session.get(Message, message_id)
        assert row is not None
        return row


async def test_a_completion_write_failure_refunds_instead_of_leaking_a_hold(
    chat_env: ChatEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = await message_service.prepare_message(
        chat_env.factory,
        user_id=chat_env.user_id,
        chat_id=UUID(chat_env.chat_id),
        content="Save failure",
        requested_model_id=None,
        client_id=None,
        provider_available=True,
    )

    async def fail_to_save(*args: object, **kwargs: object) -> None:
        raise RuntimeError("database write failed")

    monkeypatch.setattr(message_service, "_finalize_success", fail_to_save)
    events = [
        event
        async for event in message_service.stream_message(
            chat_env.factory, provider=chat_env.fake, prepared=prepared
        )
    ]

    failed = events[-1]
    assert isinstance(failed, message_service.FailedEvent)
    assert failed.code == "internal_error"
    assert failed.refunded is True

    assistant = await _assistant_row(chat_env, prepared.assistant_message_id)
    assert assistant.status == "failed"
    assert assistant.content == REPLY
    entries = await ledger_for(chat_env, prepared.assistant_message_id)
    assert [entry.kind for entry in entries] == ["reserve", "refund"]
    refreshed = await account(chat_env)
    assert (refreshed.balance, refreshed.held) == (50, 0)


async def test_another_account_cannot_read_or_write_the_chat(chat_env: ChatEnv) -> None:
    assert (await send(chat_env, clientId="c-1")).status_code == 200

    async with asgi_client(create_app(chat_env.settings, chat_provider=chat_env.fake)) as other:
        await post_signup(other, f"other-{uuid4().hex}@example.com")
        # A malformed cursor must not turn a foreign chat into a `400`.
        history = await other.get(
            f"/v1/chats/{chat_env.chat_id}/messages", params={"before": "not-a-cursor"}
        )
        stream = await other.post(
            f"/v1/chats/{chat_env.chat_id}/messages",
            json={"content": "Hijack"},
            headers={"accept": "text/event-stream"},
        )

    assert_problem(history, 404, "not_found")
    assert_problem(stream, 404, "not_found")
    rows = await messages_for(chat_env)
    assert [row.role for row in rows] == ["user", "assistant"]
    assert (await account(chat_env)).balance == 49
