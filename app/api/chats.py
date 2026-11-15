"""Chat endpoints: create, list, read, rename, and soft delete.

Every route is caller-scoped. The domain service always filters on `user_id`, so a
chat that belongs to another account is reported as `404 not_found`, exactly like
one that does not exist.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_current_auth, get_session_factory
from app.api.problems import PROBLEM_MEDIA_TYPE, ProblemDetail
from app.api.schemas.chat import (
    ChatPage,
    ChatResponse,
    ChatSummary,
    CreateChatRequest,
    UpdateChatRequest,
)
from app.db.models import Chat
from app.domain import chat as chat_service
from app.domain.auth import AuthContext

router = APIRouter(prefix="/v1", tags=["chats"])

PROBLEM = {"content": {PROBLEM_MEDIA_TYPE: {"schema": ProblemDetail.model_json_schema()}}}
CHAT_NOT_FOUND = {"description": "The chat does not exist or is not owned.", **PROBLEM}


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
