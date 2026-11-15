"""Chat and message endpoints: CRUD, history, and the streamed reply.

Every route is caller-scoped. The domain services always filter on `user_id`, so a
chat that belongs to another account is reported as `404 not_found`, exactly like
one that does not exist.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Callable
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.types import Send

from app.api.deps import (
    enforce_rate_limit,
    get_chat_provider,
    get_current_auth,
    get_rate_limiter,
    get_session_factory,
)
from app.api.problems import PROBLEM_MEDIA_TYPE, ProblemDetail
from app.api.schemas.chat import (
    ChatPage,
    ChatResponse,
    ChatSummary,
    CreateChatRequest,
    UpdateChatRequest,
)
from app.api.schemas.message import (
    CompletedPayload,
    CreditsPayload,
    DeltaPayload,
    FailedPayload,
    MessagePage,
    MessageResponse,
    MessageRole,
    MessageStatus,
    SendMessageRequest,
    StartedPayload,
    UsagePayload,
)
from app.core.rate_limit import MemoryRateLimiter
from app.db.models import Chat, Message
from app.domain import chat as chat_service
from app.domain import messages as message_service
from app.domain.auth import AuthContext
from app.domain.messages import (
    ChatStreamEvent,
    CompletedEvent,
    DeltaEvent,
    DoneEvent,
    FailedEvent,
    HeartbeatEvent,
    StartedEvent,
)
from app.providers.base import ChatProvider

router = APIRouter(prefix="/v1", tags=["chats"])

PROBLEM = {"content": {PROBLEM_MEDIA_TYPE: {"schema": ProblemDetail.model_json_schema()}}}
CHAT_NOT_FOUND = {"description": "The chat does not exist or is not owned.", **PROBLEM}

#: Chat has the tightest per-user budget in the spec: 30 messages per minute.
CHAT_MESSAGES_PER_MINUTE = 30
CHAT_RATE_WINDOW_SECONDS = 60


def _chat_response(chat: Chat) -> ChatResponse:
    return ChatResponse(
        id=chat.id,
        title=chat.title,
        model_id=chat.model_id,
        created_at=chat.created_at,
        updated_at=chat.updated_at,
    )


def _chat_summary(chat: Chat) -> ChatSummary:
    return ChatSummary(
        id=chat.id,
        title=chat.title,
        model_id=chat.model_id,
        created_at=chat.created_at,
        updated_at=chat.updated_at,
    )


def _message_response(message: Message) -> MessageResponse:
    return MessageResponse(
        id=message.id,
        # The database constrains both columns to the documented sets.
        role=cast(MessageRole, message.role),
        content=message.content,
        model_id=message.model_id,
        status=cast(MessageStatus, message.status),
        prompt_tokens=message.prompt_tokens,
        completion_tokens=message.completion_tokens,
        error_code=message.error_code,
        client_id=message.client_id,
        created_at=message.created_at,
        updated_at=message.updated_at,
    )


def _sse_frame(event: ChatStreamEvent) -> str:
    """Serialize one domain event as an SSE frame.

    Heartbeats are comments, not events, and `done` carries an empty object as
    documented. Content is JSON-encoded exactly once, so a newline in the reply
    cannot break the frame boundary.
    """
    if isinstance(event, HeartbeatEvent):
        return ": ping\n\n"
    if isinstance(event, StartedEvent):
        name = "message.started"
        data: StartedPayload | DeltaPayload | CompletedPayload | FailedPayload = StartedPayload(
            user_message_id=event.user_message_id,
            assistant_message_id=event.assistant_message_id,
            model_id=event.model_id,
        )
    elif isinstance(event, DeltaEvent):
        name = "message.delta"
        data = DeltaPayload(message_id=event.message_id, delta=event.delta)
    elif isinstance(event, CompletedEvent):
        name = "message.completed"
        data = CompletedPayload(
            message_id=event.message_id,
            content=event.content,
            usage=UsagePayload(
                prompt_tokens=event.prompt_tokens,
                completion_tokens=event.completion_tokens,
            ),
            credits=CreditsPayload(
                balance=event.balance,
                held=event.held,
                available=event.available,
                charged=event.charged,
            ),
        )
    elif isinstance(event, FailedEvent):
        name = "message.failed"
        data = FailedPayload(
            message_id=event.message_id,
            code=event.code,
            detail=event.detail,
            refunded=event.refunded,
        )
    elif isinstance(event, DoneEvent):
        return "event: done\ndata: {}\n\n"
    else:  # pragma: no cover - the union is closed
        raise TypeError(f"unknown stream event: {type(event).__name__}")

    payload = json.dumps(data.model_dump(mode="json", by_alias=True), separators=(",", ":"))
    return f"event: {name}\ndata: {payload}\n\n"


class _EventStream:
    """One reply's SSE frames, with cleanup when the client goes away.

    Starlette cancels the response task on disconnect, which may leave the body
    iterator suspended between two frames. `aclose` throws `GeneratorExit` into
    the domain generator, so the partial reply is persisted and its reservation
    is refunded. A stream cancelled before its first frame never started the
    provider, so it settles the prepared message directly.
    """

    def __init__(
        self,
        events: AsyncGenerator[ChatStreamEvent, None],
        *,
        on_disconnect: Callable[[], None],
    ) -> None:
        self._events = events
        self._on_disconnect = on_disconnect
        self._started = False

    def __aiter__(self) -> _EventStream:
        return self

    async def __anext__(self) -> str:
        self._started = True
        return _sse_frame(await anext(self._events))

    async def aclose(self) -> None:
        if not self._started:
            self._on_disconnect()
            return
        await self._events.aclose()


class _SseResponse(StreamingResponse):
    """Closes the event iterator when the response task finishes or is cancelled.

    Without this, an iterator suspended between frames is only closed at garbage
    collection, so a disconnected reply keeps its Credits on hold in the meantime.
    """

    async def stream_response(self, send: Send) -> None:
        try:
            await super().stream_response(send)
        finally:
            close = getattr(self.body_iterator, "aclose", None)
            if close is not None:
                await close()


@router.get(
    "/chats",
    response_model=ChatPage,
    summary="List chats",
    description=(
        "Returns the caller's live chats, most recently updated first, as an "
        "opaque-cursor page. Soft-deleted chats are excluded."
    ),
)
async def list_chats(
    context: Annotated[AuthContext, Depends(get_current_auth)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    cursor: Annotated[str | None, Query(description="Opaque cursor from `nextCursor`.")] = None,
    limit: Annotated[
        int,
        Query(ge=1, le=chat_service.CHAT_MAX_PAGE_SIZE, description="Items per page."),
    ] = chat_service.CHAT_PAGE_SIZE,
) -> ChatPage:
    chats, next_cursor = await chat_service.list_chats(
        session_factory,
        user_id=context.user.id,
        cursor=cursor,
        limit=limit,
    )
    return ChatPage(items=[_chat_summary(chat) for chat in chats], next_cursor=next_cursor)


@router.post(
    "/chats",
    response_model=ChatResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a chat",
    description=(
        "Creates a chat for an available `chat` model. The title is optional; "
        "omitting it leaves the placeholder until the first message."
    ),
    responses={422: {"description": "The model is unknown or unavailable.", **PROBLEM}},
)
async def create_chat(
    body: CreateChatRequest,
    context: Annotated[AuthContext, Depends(get_current_auth)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> ChatResponse:
    chat = await chat_service.create_chat(
        session_factory,
        user_id=context.user.id,
        model_id=body.model_id,
        title=body.title,
    )
    return _chat_response(chat)


@router.get(
    "/chats/{chat_id}",
    response_model=ChatResponse,
    summary="Get a chat",
    description="Returns one live chat owned by the caller.",
    responses={404: CHAT_NOT_FOUND},
)
async def get_chat(
    chat_id: UUID,
    context: Annotated[AuthContext, Depends(get_current_auth)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> ChatResponse:
    chat = await chat_service.get_chat(
        session_factory,
        user_id=context.user.id,
        chat_id=chat_id,
    )
    return _chat_response(chat)


@router.patch(
    "/chats/{chat_id}",
    response_model=ChatResponse,
    summary="Rename a chat",
    description="Sets an explicit title and bumps the chat's `updatedAt`.",
    responses={404: CHAT_NOT_FOUND},
)
async def update_chat(
    chat_id: UUID,
    body: UpdateChatRequest,
    context: Annotated[AuthContext, Depends(get_current_auth)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> ChatResponse:
    chat = await chat_service.rename_chat(
        session_factory,
        user_id=context.user.id,
        chat_id=chat_id,
        title=body.title,
    )
    return _chat_response(chat)


@router.delete(
    "/chats/{chat_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a chat",
    description=(
        "Soft-deletes the chat: it stops appearing in listings and reads, but the "
        "row remains. Deleting twice is a no-op."
    ),
    responses={404: CHAT_NOT_FOUND},
)
async def delete_chat(
    chat_id: UUID,
    context: Annotated[AuthContext, Depends(get_current_auth)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> Response:
    await chat_service.delete_chat(
        session_factory,
        user_id=context.user.id,
        chat_id=chat_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/chats/{chat_id}/messages",
    response_model=MessagePage,
    summary="List chat messages",
    description=(
        "Returns one page of the chat's persisted messages, newest first, as an "
        "opaque-cursor page. Failed and partial assistant turns are included."
    ),
    responses={404: CHAT_NOT_FOUND},
)
async def list_messages(
    chat_id: UUID,
    context: Annotated[AuthContext, Depends(get_current_auth)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    before: Annotated[
        str | None,
        Query(description="Opaque cursor for older messages."),
    ] = None,
    limit: Annotated[
        int,
        Query(ge=1, le=message_service.MESSAGE_MAX_PAGE_SIZE, description="Items per page."),
    ] = message_service.MESSAGE_PAGE_SIZE,
) -> MessagePage:
    rows, next_cursor = await message_service.list_messages(
        session_factory,
        user_id=context.user.id,
        chat_id=chat_id,
        before=before,
        limit=limit,
    )
    return MessagePage(
        items=[_message_response(row) for row in rows],
        next_cursor=next_cursor,
    )


@router.post(
    "/chats/{chat_id}/messages",
    response_class=StreamingResponse,
    summary="Send a message",
    description=(
        "Persists the user turn, reserves Credits, and streams the assistant reply "
        "as `text/event-stream`. The reply ends with `message.completed` then "
        "`done`; a failure ends with `message.failed` and refunds the reservation."
    ),
    responses={
        200: {
            "description": "Server-sent events for this reply.",
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        },
        404: CHAT_NOT_FOUND,
        409: {
            "description": "The message duplicates an earlier client id (`duplicate_message`).",
            **PROBLEM,
        },
        422: {"description": "The chat's model is not available.", **PROBLEM},
    },
)
async def send_message(
    chat_id: UUID,
    body: SendMessageRequest,
    context: Annotated[AuthContext, Depends(get_current_auth)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    limiter: Annotated[MemoryRateLimiter, Depends(get_rate_limiter)],
    provider: Annotated[ChatProvider | None, Depends(get_chat_provider)],
) -> StreamingResponse:
    enforce_rate_limit(
        limiter,
        f"chat-message:{context.user.id}",
        limit=CHAT_MESSAGES_PER_MINUTE,
        window_seconds=CHAT_RATE_WINDOW_SECONDS,
    )
    prepared = await message_service.prepare_message(
        session_factory,
        user_id=context.user.id,
        chat_id=chat_id,
        content=body.content,
        requested_model_id=body.model_id,
        client_id=body.client_id,
        provider_available=provider is not None,
    )
    if provider is None:
        # `prepare_message` rejects an unconfigured provider before any write.
        raise AssertionError("prepare_message accepted an unconfigured provider")
    events = message_service.stream_message(
        session_factory,
        provider=provider,
        prepared=prepared,
    )
    event_stream = _EventStream(
        events,
        on_disconnect=lambda: message_service.abort_message(session_factory, prepared),
    )
    return _SseResponse(
        event_stream,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )
