"""Message persistence and the streamed chat use case.

`prepare_message` writes the user turn and an assistant placeholder and reserves
Credits in one transaction, before any provider call. `stream_message` then
yields typed events while the provider speaks, persists the final or partial
reply, and commits or refunds the reservation exactly once.

The generator owns no request/session state: each database step opens a short
transaction, so neither a slow provider nor a slow client holds a connection.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, cast
from uuid import UUID

import structlog
from sqlalchemy import select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.problems import ApiError, ErrorCode
from app.core.cursor import InvalidCursorError, decode_keyset, encode_keyset
from app.core.ids import uuid7
from app.db.models import Chat, CreditAccount, Message, Model
from app.db.models.common import utcnow
from app.db.session import session_scope
from app.domain import credits
from app.domain.chat import CHAT_NOT_FOUND_DETAIL, title_from_message
from app.providers.base import (
    ChatDelta,
    ChatParams,
    ChatProvider,
    ChatUsage,
    ProviderError,
    ProviderMessage,
    ProviderModel,
)

log = structlog.get_logger(__name__)

MESSAGE_PAGE_SIZE = 50
MESSAGE_MAX_PAGE_SIZE = 100

#: How often an idle stream emits a `: ping` comment. Proxies and browsers close
#: silent connections long before a slow model answers.
HEARTBEAT_SECONDS = 15.0

MODEL_UNAVAILABLE_DETAIL = "That model is not available."
MODEL_MISMATCH_DETAIL = "The message model does not match this chat."
DUPLICATE_MESSAGE_DETAIL = "That message was already sent."
INVALID_CURSOR_DETAIL = "The cursor is not valid."
STREAM_FAILED_DETAIL = "The model could not finish the reply."
STREAM_SAVE_FAILED_DETAIL = "The reply could not be saved."

#: Ledger reason. It names the use case, never the prompt or the completion.
CHAT_CREDIT_REASON = "Chat message"

#: The assistant placeholder is stamped just after its user turn, so
#: `(created_at, id)` has one stable chronological order even within a request.
_ASSISTANT_OFFSET = timedelta(microseconds=1)

MessageRole = Literal["system", "user", "assistant"]


@dataclass(frozen=True)
class PreparedMessage:
    """Everything a stream needs after the preflight transaction commits."""

    chat_id: UUID
    user_id: UUID
    user_message_id: UUID
    assistant_message_id: UUID
    model_slug: str
    provider_model_id: str
    credit_cost: int
    messages: tuple[ProviderMessage, ...]


@dataclass(frozen=True)
class StartedEvent:
    user_message_id: UUID
    assistant_message_id: UUID
    model_id: str


@dataclass(frozen=True)
class DeltaEvent:
    message_id: UUID
    delta: str


@dataclass(frozen=True)
class CompletedEvent:
    message_id: UUID
    content: str
    prompt_tokens: int | None
    completion_tokens: int | None
    balance: int
    held: int
    available: int
    charged: int


@dataclass(frozen=True)
class FailedEvent:
    message_id: UUID
    code: str
    detail: str
    refunded: bool


@dataclass(frozen=True)
class DoneEvent:
    """The normal end of an event stream. A failure ends with `FailedEvent`."""


@dataclass(frozen=True)
class HeartbeatEvent:
    """An idle signal, serialized as a `: ping` comment rather than an event."""


ChatStreamEvent = (
    StartedEvent | DeltaEvent | CompletedEvent | FailedEvent | DoneEvent | HeartbeatEvent
)


def _decode_message_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        return decode_keyset(cursor)
    except InvalidCursorError as exc:
        raise ApiError(ErrorCode.INVALID_REQUEST, 400, detail=INVALID_CURSOR_DETAIL) from exc


def _provider_messages(rows: Sequence[Message]) -> tuple[ProviderMessage, ...]:
    """Complete turns only: a failed partial reply is not model context."""
    return tuple(
        ProviderMessage(role=cast(MessageRole, row.role), content=row.content) for row in rows
    )


async def _require_chat(
    session: AsyncSession,
    *,
    user_id: UUID,
    chat_id: UUID,
    lock: bool = False,
) -> Chat:
    statement = select(Chat).where(
        Chat.id == chat_id,
        Chat.user_id == user_id,
        Chat.deleted_at.is_(None),
    )
    if lock:
        statement = statement.with_for_update()
    chat = await session.scalar(statement)
    if chat is None:
        raise ApiError(ErrorCode.NOT_FOUND, 404, detail=CHAT_NOT_FOUND_DETAIL)
    return chat


async def prepare_message(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: UUID,
    chat_id: UUID,
    content: str,
    requested_model_id: str | None,
    client_id: str | None,
    provider_available: bool,
) -> PreparedMessage:
    """Persist the user turn and assistant placeholder, then reserve Credits.

    Raises `404` for a missing or foreign chat, `422` when the chat's model is no
    longer servable, `400` when `modelId` disagrees with the chat, `409` for a
    duplicate `clientId`, and `409 insufficient_credits` when the reservation
    cannot be covered. Nothing is written unless every check passes.
    """
    try:
        async with session_scope(session_factory) as session:
            chat = await _require_chat(session, user_id=user_id, chat_id=chat_id, lock=True)
            if requested_model_id is not None and requested_model_id != chat.model_id:
                raise ApiError(ErrorCode.INVALID_REQUEST, 400, detail=MODEL_MISMATCH_DETAIL)

            model = await session.get(Model, chat.model_id)
            if (
                not provider_available
                or model is None
                or model.modality != "chat"
                or not model.available
            ):
                raise ApiError(ErrorCode.MODEL_UNAVAILABLE, 422, detail=MODEL_UNAVAILABLE_DETAIL)

            if client_id is not None:
                duplicate = await session.scalar(
                    select(Message.id).where(
                        Message.chat_id == chat.id,
                        Message.client_id == client_id,
                    )
                )
                if duplicate is not None:
                    raise ApiError(
                        ErrorCode.DUPLICATE_MESSAGE, 409, detail=DUPLICATE_MESSAGE_DETAIL
                    )

            history = list(
                await session.scalars(
                    select(Message)
                    .where(Message.chat_id == chat.id, Message.status == "complete")
                    .order_by(Message.created_at, Message.id)
                )
            )

            now = utcnow()
            user_message = Message(
                id=uuid7(),
                chat_id=chat.id,
                user_id=user_id,
                role="user",
                content=content,
                model_id=None,
                status="complete",
                client_id=client_id,
                created_at=now,
                updated_at=now,
            )
            assistant_message = Message(
                id=uuid7(),
                chat_id=chat.id,
                user_id=user_id,
                role="assistant",
                content="",
                model_id=chat.model_id,
                status="streaming",
                created_at=now + _ASSISTANT_OFFSET,
                updated_at=now + _ASSISTANT_OFFSET,
            )
            session.add_all([user_message, assistant_message])

            if not chat.title_is_custom and not any(row.role == "user" for row in history):
                chat.title = title_from_message(content)
            chat.updated_at = now

            await credits.reserve(
                session,
                user_id=user_id,
                amount=model.credit_cost,
                reason=CHAT_CREDIT_REASON,
                ref_type="chat_message",
                ref_id=assistant_message.id,
            )
            await session.flush()

            return PreparedMessage(
                chat_id=chat.id,
                user_id=user_id,
                user_message_id=user_message.id,
                assistant_message_id=assistant_message.id,
                model_slug=model.id,
                provider_model_id=model.provider_model_id,
                credit_cost=model.credit_cost,
                messages=(
                    *_provider_messages(history),
                    ProviderMessage(role="user", content=content),
                ),
            )
    except IntegrityError as exc:
        if "uq_messages_chat_id_client_id" in str(exc.orig):
            raise ApiError(
                ErrorCode.DUPLICATE_MESSAGE, 409, detail=DUPLICATE_MESSAGE_DETAIL
            ) from exc
        raise


async def list_messages(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: UUID,
    chat_id: UUID,
    before: str | None,
    limit: int,
) -> tuple[list[Message], str | None]:
    """Return one newest-first page of the chat's messages, including failures."""
    statement = (
        select(Message)
        .where(Message.chat_id == chat_id, Message.user_id == user_id)
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(limit + 1)
    )
    async with session_scope(session_factory) as session:
        # Ownership is checked first, so a foreign chat with a malformed cursor
        # is still `404`, exactly like a missing one.
        await _require_chat(session, user_id=user_id, chat_id=chat_id)
        if before is not None:
            after = _decode_message_cursor(before)
            statement = statement.where(tuple_(Message.created_at, Message.id) < after)
        rows = list(await session.scalars(statement))

    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = encode_keyset(page[-1].created_at, page[-1].id) if has_more and page else None
    return page, next_cursor


async def _lock_assistant(
    session: AsyncSession, *, user_id: UUID, assistant_message_id: UUID
) -> Message | None:
    return await session.scalar(
        select(Message)
        .where(
            Message.id == assistant_message_id,
            Message.user_id == user_id,
            Message.role == "assistant",
        )
        .with_for_update()
    )


async def _finalize_success(
    session_factory: async_sessionmaker[AsyncSession],
    prepared: PreparedMessage,
    *,
    content: str,
    usage: ChatUsage | None,
) -> CreditAccount:
    """Persist the completed reply and commit its reservation in one transaction."""
    async with session_scope(session_factory) as session:
        assistant = await _lock_assistant(
            session,
            user_id=prepared.user_id,
            assistant_message_id=prepared.assistant_message_id,
        )
        if assistant is None or assistant.status != "streaming":
            raise RuntimeError("the assistant message is not streaming")
        now = utcnow()
        assistant.content = content
        assistant.status = "complete"
        assistant.prompt_tokens = usage.prompt_tokens if usage is not None else None
        assistant.completion_tokens = usage.completion_tokens if usage is not None else None
        assistant.updated_at = now
        # Lock order is chat -> account, matching `prepare_message`. Reversing it
        # here could deadlock against a second message arriving for the same chat.
        await _touch_chat(session, chat_id=prepared.chat_id, now=now)
        account = await credits.commit(
            session,
            user_id=prepared.user_id,
            amount=prepared.credit_cost,
            reason=CHAT_CREDIT_REASON,
            ref_type="chat_message",
            ref_id=prepared.assistant_message_id,
        )
        await session.flush()
        return account


async def _finalize_failure(
    session_factory: async_sessionmaker[AsyncSession],
    prepared: PreparedMessage,
    *,
    content: str,
    code: ErrorCode,
) -> bool:
    """Persist partial content, mark the message failed, and refund.

    Returns whether a reservation was refunded. A message that already reached a
    terminal state is left alone, so a second finalize cannot refund twice.
    """
    async with session_scope(session_factory) as session:
        assistant = await _lock_assistant(
            session,
            user_id=prepared.user_id,
            assistant_message_id=prepared.assistant_message_id,
        )
        if assistant is None or assistant.status != "streaming":
            return False
        now = utcnow()
        assistant.content = content
        assistant.status = "failed"
        assistant.error_code = code.value
        assistant.updated_at = now
        await _touch_chat(session, chat_id=prepared.chat_id, now=now)
        await credits.refund(
            session,
            user_id=prepared.user_id,
            amount=prepared.credit_cost,
            reason=CHAT_CREDIT_REASON,
            ref_type="chat_message",
            ref_id=prepared.assistant_message_id,
        )
        await session.flush()
        return True


async def _finalize_cancelled(
    session_factory: async_sessionmaker[AsyncSession],
    prepared: PreparedMessage,
    *,
    content: str,
) -> bool:
    """Persist a disconnected reply as cancelled and refund its reservation.

    Returns whether this call refunded. A message that already reached a
    terminal state is left alone, so an abort that races completion cannot move
    Credits twice.
    """
    async with session_scope(session_factory) as session:
        assistant = await _lock_assistant(
            session,
            user_id=prepared.user_id,
            assistant_message_id=prepared.assistant_message_id,
        )
        if assistant is None or assistant.status != "streaming":
            return False
        now = utcnow()
        assistant.content = content
        assistant.status = "cancelled"
        assistant.updated_at = now
        await _touch_chat(session, chat_id=prepared.chat_id, now=now)
        await credits.refund(
            session,
            user_id=prepared.user_id,
            amount=prepared.credit_cost,
            reason=CHAT_CREDIT_REASON,
            ref_type="chat_message",
            ref_id=prepared.assistant_message_id,
        )
        await session.flush()
        return True


async def _touch_chat(session: AsyncSession, *, chat_id: UUID, now: datetime) -> None:
    """Bump the chat and take its row lock, keeping chat -> account order."""
    chat = await session.scalar(select(Chat).where(Chat.id == chat_id).with_for_update())
    if chat is not None:
        chat.updated_at = now


async def _provider_events(
    provider: ChatProvider, prepared: PreparedMessage
) -> AsyncGenerator[ChatDelta | HeartbeatEvent, None]:
    """Yield provider deltas, or a heartbeat after `HEARTBEAT_SECONDS` of silence.

    `anext` stays pending across a heartbeat instead of being cancelled, so a
    slow provider is not torn down just because it is quiet. The generator is
    closed explicitly when the consumer stops early.
    """
    iterator = provider.stream_chat(
        messages=prepared.messages,
        model=ProviderModel(
            slug=prepared.model_slug,
            provider_model_id=prepared.provider_model_id,
        ),
        params=ChatParams(),
    )
    pending = asyncio.ensure_future(anext(iterator))
    try:
        while True:
            done, _ = await asyncio.wait({pending}, timeout=HEARTBEAT_SECONDS)
            if not done:
                yield HeartbeatEvent()
                continue
            try:
                delta = pending.result()
            except StopAsyncIteration:
                return
            pending = asyncio.ensure_future(anext(iterator))
            yield delta
    finally:
        if not pending.done():
            pending.cancel()
            with suppress(asyncio.CancelledError):
                await pending
        close = getattr(iterator, "aclose", None)
        if close is not None:
            await close()


async def _emit_failure(
    session_factory: async_sessionmaker[AsyncSession],
    prepared: PreparedMessage,
    *,
    content: str,
    code: ErrorCode,
    detail: str,
) -> FailedEvent:
    try:
        refunded = await _finalize_failure(session_factory, prepared, content=content, code=code)
    except Exception as exc:
        log.error(
            "chat.stream_finalize_failed",
            chat_id=str(prepared.chat_id),
            message_id=str(prepared.assistant_message_id),
            error=type(exc).__name__,
        )
        refunded = False
    return FailedEvent(
        message_id=prepared.assistant_message_id,
        code=code.value,
        detail=detail,
        refunded=refunded,
    )


#: Detached abort cleanups. A disconnected request task is already cancelled, so
#: every await in it raises again; settling the message has to run elsewhere.
#: The set keeps a strong reference to each task until it finishes.
_abort_tasks: set[asyncio.Task[None]] = set()


def _abort_finished(task: asyncio.Task[None]) -> None:
    _abort_tasks.discard(task)
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        log.error("chat.abort_finalize_failed", error=type(error).__name__)


async def _abort_cleanup(
    session_factory: async_sessionmaker[AsyncSession],
    prepared: PreparedMessage,
    *,
    content: str,
    provider_events: AsyncGenerator[ChatDelta | HeartbeatEvent, None] | None,
) -> None:
    """Close an abandoned provider stream after settling the message and hold."""
    try:
        await _finalize_cancelled(session_factory, prepared, content=content)
    except Exception as exc:
        log.error(
            "chat.abort_refund_failed",
            chat_id=str(prepared.chat_id),
            message_id=str(prepared.assistant_message_id),
            error=type(exc).__name__,
        )
        raise
    finally:
        if provider_events is not None:
            # The provider generator may already be closing in the cancelled
            # task; an `aclose` race is harmless once the database step ran.
            with suppress(Exception):
                await provider_events.aclose()


def _spawn_abort_cleanup(
    session_factory: async_sessionmaker[AsyncSession],
    prepared: PreparedMessage,
    *,
    content: str,
    provider_events: AsyncGenerator[ChatDelta | HeartbeatEvent, None] | None,
) -> None:
    task = asyncio.create_task(
        _abort_cleanup(
            session_factory,
            prepared,
            content=content,
            provider_events=provider_events,
        )
    )
    _abort_tasks.add(task)
    task.add_done_callback(_abort_finished)


def abort_message(
    session_factory: async_sessionmaker[AsyncSession],
    prepared: PreparedMessage,
    *,
    content: str = "",
) -> None:
    """Settle a reply whose client left before its stream produced a frame."""
    _spawn_abort_cleanup(
        session_factory,
        prepared,
        content=content,
        provider_events=None,
    )


async def drain_abort_finalizers() -> None:
    """Wait for detached abort cleanups. Runs on shutdown and in tests."""
    while _abort_tasks:
        await asyncio.gather(*tuple(_abort_tasks), return_exceptions=True)


async def stream_message(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    provider: ChatProvider,
    prepared: PreparedMessage,
) -> AsyncGenerator[ChatStreamEvent, None]:
    """Stream one reply and persist its outcome.

    A provider failure ends the stream with `message.failed` and no `done`; a
    successful reply ends with `message.completed` then `done`. A client that
    goes away closes the generator, which persists the partial reply as
    `cancelled` and refunds the reservation.
    """
    chunks: list[str] = []
    usage: ChatUsage | None = None
    provider_events = _provider_events(provider, prepared)
    try:
        yield StartedEvent(
            user_message_id=prepared.user_message_id,
            assistant_message_id=prepared.assistant_message_id,
            model_id=prepared.model_slug,
        )

        try:
            async for item in provider_events:
                if isinstance(item, HeartbeatEvent):
                    yield item
                    continue
                if item.usage is not None:
                    usage = item.usage
                if item.text:
                    chunks.append(item.text)
                    yield DeltaEvent(message_id=prepared.assistant_message_id, delta=item.text)
        except ProviderError as exc:
            yield await _emit_failure(
                session_factory,
                prepared,
                content="".join(chunks),
                code=exc.code,
                detail=exc.detail,
            )
            return
        except Exception as exc:
            log.error(
                "chat.stream_provider_failed",
                chat_id=str(prepared.chat_id),
                message_id=str(prepared.assistant_message_id),
                error=type(exc).__name__,
            )
            yield await _emit_failure(
                session_factory,
                prepared,
                content="".join(chunks),
                code=ErrorCode.PROVIDER_ERROR,
                detail=STREAM_FAILED_DETAIL,
            )
            return

        content = "".join(chunks)
        try:
            account = await _finalize_success(
                session_factory,
                prepared,
                content=content,
                usage=usage,
            )
        except Exception as exc:
            log.error(
                "chat.stream_completion_failed",
                chat_id=str(prepared.chat_id),
                message_id=str(prepared.assistant_message_id),
                error=type(exc).__name__,
            )
            yield await _emit_failure(
                session_factory,
                prepared,
                content=content,
                code=ErrorCode.INTERNAL_ERROR,
                detail=STREAM_SAVE_FAILED_DETAIL,
            )
            return

        yield CompletedEvent(
            message_id=prepared.assistant_message_id,
            content=content,
            prompt_tokens=usage.prompt_tokens if usage is not None else None,
            completion_tokens=usage.completion_tokens if usage is not None else None,
            balance=account.balance,
            held=account.held,
            available=credits.available(account),
            charged=prepared.credit_cost,
        )
        yield DoneEvent()
    except (GeneratorExit, asyncio.CancelledError):
        # The response task is unwinding (client disconnect), so an await on the
        # database here would be cancelled again. Settle it from a detached task.
        _spawn_abort_cleanup(
            session_factory,
            prepared,
            content="".join(chunks),
            provider_events=provider_events,
        )
        raise
