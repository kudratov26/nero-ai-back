"""Explore endpoints: the public gallery and authenticated likes.

The two reads are public, so a signed-out browser can browse the gallery. They
resolve a session when one is present only to fill `likedByMe`. The two writes
require a session but do not scope the post to the caller: likes are a
cross-account relation by design, so another account's published post is the
normal target rather than a `404`.
"""

from __future__ import annotations

from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_current_auth, get_optional_auth, get_session_factory
from app.api.problems import PROBLEM_MEDIA_TYPE, ProblemDetail
from app.api.schemas.catalog import Modality
from app.api.schemas.explore import (
    PostAssetResponse,
    PostAuthorResponse,
    PostPage,
    PostResponse,
)
from app.domain import explore as explore_service
from app.domain.auth import AuthContext
from app.domain.explore import PostView

router = APIRouter(prefix="/v1/explore", tags=["explore"])

PROBLEM = {"content": {PROBLEM_MEDIA_TYPE: {"schema": ProblemDetail.model_json_schema()}}}
POST_NOT_FOUND = {"description": "The post does not exist or is not published.", **PROBLEM}


def _post_response(view: PostView) -> PostResponse:
    asset = None
    if view.asset is not None:
        asset = PostAssetResponse(
            id=view.asset.id,
            url=f"/v1/assets/{view.asset.id}",
            mime_type=view.asset.mime_type,
            width=view.asset.width,
            height=view.asset.height,
            duration_ms=view.asset.duration_ms,
        )
    author = None
    if view.author is not None:
        author = PostAuthorResponse(
            id=view.author.id,
            display_name=view.author.display_name,
            initials=view.author.initials,
        )
    return PostResponse(
        id=view.post.id,
        modality=cast(Modality, view.post.modality),
        title=view.post.title,
        prompt=view.post.prompt,
        model_id=view.post.model_id,
        params=dict(view.post.params),
        author=author,
        asset=asset,
        like_count=view.like_count,
        liked_by_me=view.liked_by_me,
        created_at=view.post.created_at,
    )


@router.get(
    "/posts",
    response_model=PostPage,
    summary="List Explore posts",
    description=(
        "Returns published posts, newest first, as an opaque-cursor page. Public; "
        "`likedByMe` is false for a signed-out caller. `modality` and `modelId` "
        "filter the gallery before pagination."
    ),
)
async def list_posts(
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    auth: Annotated[AuthContext | None, Depends(get_optional_auth)],
    modality: Annotated[Modality | None, Query(description="chat, image, or video")] = None,
    model_id: Annotated[
        str | None, Query(alias="modelId", max_length=120, description="Model id.")
    ] = None,
    cursor: Annotated[str | None, Query(description="Opaque cursor from `nextCursor`.")] = None,
    limit: Annotated[
        int,
        Query(ge=1, le=explore_service.POST_MAX_PAGE_SIZE, description="Items per page."),
    ] = explore_service.POST_PAGE_SIZE,
) -> PostPage:
    views, next_cursor = await explore_service.list_posts(
        session_factory,
        user_id=None if auth is None else auth.user.id,
        modality=modality,
        model_id=model_id,
        cursor=cursor,
        limit=limit,
    )
    return PostPage(items=[_post_response(view) for view in views], next_cursor=next_cursor)


@router.get(
    "/posts/{post_id}",
    response_model=PostResponse,
    summary="Get an Explore post",
    description=(
        "Returns one published post. Public; `likedByMe` is false for a signed-out "
        "caller. An unknown or unpublished id is 404."
    ),
    responses={404: POST_NOT_FOUND},
)
async def get_post(
    post_id: UUID,
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    auth: Annotated[AuthContext | None, Depends(get_optional_auth)],
) -> PostResponse:
    view = await explore_service.get_post(
        session_factory,
        post_id=post_id,
        user_id=None if auth is None else auth.user.id,
    )
    return _post_response(view)


@router.post(
    "/posts/{post_id}/like",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Like a post",
    description=(
        "Likes a published post. Repeated calls succeed with 204, and the like "
        "count is unchanged. Requires a session; an unknown or unpublished post "
        "is 404."
    ),
    responses={404: POST_NOT_FOUND},
)
async def like_post(
    post_id: UUID,
    context: Annotated[AuthContext, Depends(get_current_auth)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> Response:
    await explore_service.like_post(
        session_factory,
        post_id=post_id,
        user_id=context.user.id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/posts/{post_id}/like",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Unlike a post",
    description=(
        "Removes the caller's like from a published post. Repeated calls succeed "
        "with 204, including when no like existed. Requires a session; an unknown "
        "or unpublished post is 404."
    ),
    responses={404: POST_NOT_FOUND},
)
async def unlike_post(
    post_id: UUID,
    context: Annotated[AuthContext, Depends(get_current_auth)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> Response:
    await explore_service.unlike_post(
        session_factory,
        post_id=post_id,
        user_id=context.user.id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
