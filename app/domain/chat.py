"""Chat use cases: create, list, read, rename, and soft delete.

Every query is scoped by `user_id`, so another user's chat is indistinguishable
from one that does not exist: both are `404 not_found`. That is the only safe
answer, because a `403` would confirm the row exists.

Chats own their transaction here. Nothing in this module calls a provider or
moves Credits; that arrives with messages in B3.3.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.problems import ApiError, ErrorCode
from app.core.cursor import InvalidCursorError, decode_keyset, encode_keyset
from app.db.models import Chat, Model
from app.db.models.chat import DEFAULT_TITLE, MAX_TITLE_SOURCE_LENGTH
from app.db.session import session_scope

CHAT_PAGE_SIZE = 20
CHAT_MAX_PAGE_SIZE = 100

CHAT_NOT_FOUND_DETAIL = "That chat does not exist."
MODEL_UNAVAILABLE_DETAIL = "That model is not available."
INVALID_CURSOR_DETAIL = "The cursor is not valid."

_WHITESPACE = re.compile(r"\s+")


def title_from_message(content: str) -> str:
    """Derive a chat title from the first user message.

    Whitespace is collapsed and the result is trimmed to the documented 60
    characters, so a multi-line paste cannot store a wall of text as a title. An
    empty message falls back to the default title rather than failing: the title
    is cosmetic, and the message itself is validated elsewhere.
    """
    collapsed = _WHITESPACE.sub(" ", content).strip()
    if not collapsed:
        return DEFAULT_TITLE
    if len(collapsed) <= MAX_TITLE_SOURCE_LENGTH:
        return collapsed
    # Cut on a word boundary when one is close to the limit; never exceed it.
    cut = collapsed[:MAX_TITLE_SOURCE_LENGTH].rstrip()
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut or DEFAULT_TITLE


def _decode_chat_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        return decode_keyset(cursor)
    except InvalidCursorError as exc:
        raise ApiError(ErrorCode.INVALID_REQUEST, 400, detail=INVALID_CURSOR_DETAIL) from exc


async def _require_chat_model(session: AsyncSession, model_id: str) -> Model:
    """Return the chat model, or reject a model the server cannot serve.

    Unknown and unadvertised models get the same answer on purpose: the catalog
    already hides models whose provider is not configured, and a create route
    must not become a way to discover them.
    """
    model = await session.get(Model, model_id)
    if model is None or model.modality != "chat" or not model.available:
        raise ApiError(ErrorCode.MODEL_UNAVAILABLE, 422, detail=MODEL_UNAVAILABLE_DETAIL)
    return model


async def create_chat(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: UUID,
    model_id: str,
    title: str | None,
) -> Chat:
    """Create a chat owned by the caller.

    The model must be an available `chat` model. The title is optional; when it is
    absent the placeholder is `New chat` and B3.3 replaces it with the first
    message's opening words.
    """
    async with session_scope(session_factory) as session:
        await _require_chat_model(session, model_id)
        chat = Chat(
            user_id=user_id,
            model_id=model_id,
            title=title or DEFAULT_TITLE,
            title_is_custom=title is not None,
        )
        session.add(chat)
        await session.flush()
        return chat


async def list_chats(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: UUID,
    cursor: str | None,
    limit: int,
) -> tuple[list[Chat], str | None]:
    """Return one page of the caller's live chats, most recently active first."""
    statement = (
        select(Chat)
        .where(Chat.user_id == user_id, Chat.deleted_at.is_(None))
        .order_by(Chat.updated_at.desc(), Chat.id.desc())
        .limit(limit + 1)
    )
    if cursor is not None:
        after = _decode_chat_cursor(cursor)
        statement = statement.where(tuple_(Chat.updated_at, Chat.id) < after)

    async with session_scope(session_factory) as session:
        rows = list(await session.scalars(statement))

    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = encode_keyset(page[-1].updated_at, page[-1].id) if has_more and page else None
    return page, next_cursor


async def get_chat(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: UUID,
    chat_id: UUID,
) -> Chat:
    """Return one live chat owned by the caller, or `404 not_found`."""
    async with session_scope(session_factory) as session:
        chat = await session.scalar(
            select(Chat).where(
                Chat.id == chat_id,
                Chat.user_id == user_id,
                Chat.deleted_at.is_(None),
            )
        )
        if chat is None:
            raise ApiError(ErrorCode.NOT_FOUND, 404, detail=CHAT_NOT_FOUND_DETAIL)
        return chat


async def rename_chat(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: UUID,
    chat_id: UUID,
    title: str,
) -> Chat:
    """Set an explicit title. Renaming bumps `updated_at`, so the chat moves up."""
    async with session_scope(session_factory) as session:
        chat = await session.scalar(
            select(Chat)
            .where(
                Chat.id == chat_id,
                Chat.user_id == user_id,
                Chat.deleted_at.is_(None),
            )
            .with_for_update()
        )
        if chat is None:
            raise ApiError(ErrorCode.NOT_FOUND, 404, detail=CHAT_NOT_FOUND_DETAIL)
        chat.title = title
        chat.title_is_custom = True
        chat.updated_at = datetime.now(tz=UTC)
        await session.flush()
        return chat


async def delete_chat(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: UUID,
    chat_id: UUID,
) -> None:
    """Soft-delete the chat.

    Deleting an already-deleted chat is a no-op rather than a `404`, so a retried
    delete succeeds. A chat that never existed, or belongs to someone else, is
    still `404 not_found`.
    """
    async with session_scope(session_factory) as session:
        chat = await session.scalar(
            select(Chat).where(Chat.id == chat_id, Chat.user_id == user_id).with_for_update()
        )
        if chat is None:
            raise ApiError(ErrorCode.NOT_FOUND, 404, detail=CHAT_NOT_FOUND_DETAIL)
        if chat.deleted_at is None:
            chat.deleted_at = datetime.now(tz=UTC)
            await session.flush()
