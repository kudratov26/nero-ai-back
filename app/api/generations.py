"""Generation endpoints: create, list, read, and cancel.

Every route is caller-scoped. The create route requires an `Idempotency-Key`
header and reserves Credits in the same transaction as the Generation row, so a
retry cannot queue the same work twice. Create answers `202`: the row is queued,
not finished.
"""

from __future__ import annotations

from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import (
    enforce_rate_limit,
    get_current_auth,
    get_rate_limiter,
    get_session_factory,
)
from app.api.problems import PROBLEM_MEDIA_TYPE, ApiError, ErrorCode, ProblemDetail
from app.api.schemas.generation import (
    CreateGenerationRequest,
    GenerationAssetResponse,
    GenerationPage,
    GenerationResponse,
    GenerationStatus,
)
from app.core.rate_limit import RateLimiter
from app.db.models.idempotency import MAX_KEY_LENGTH
from app.domain import generations as generation_service
from app.domain.auth import AuthContext
from app.domain.generations import GenerationModality, GenerationView

router = APIRouter(prefix="/v1", tags=["generations"])

PROBLEM = {"content": {PROBLEM_MEDIA_TYPE: {"schema": ProblemDetail.model_json_schema()}}}
GENERATION_NOT_FOUND = {
    "description": "The generation does not exist or is not owned.",
    **PROBLEM,
}

#: The documented per-user budget: 10 Generation creates per minute.
GENERATIONS_PER_MINUTE = 10
GENERATION_RATE_WINDOW_SECONDS = 60

IDEMPOTENCY_KEY_REQUIRED_DETAIL = "An Idempotency-Key header is required."
IDEMPOTENCY_KEY_INVALID_DETAIL = "The Idempotency-Key header is not valid."


def _idempotency_key(value: str | None) -> str:
    """Validate the header at the route, as the contract requires."""
    if value is None:
        raise ApiError(
            ErrorCode.INVALID_REQUEST,
            400,
            detail=IDEMPOTENCY_KEY_REQUIRED_DETAIL,
        )
    key = value.strip()
    if not key or len(key) > MAX_KEY_LENGTH:
        raise ApiError(
            ErrorCode.INVALID_REQUEST,
            400,
            detail=IDEMPOTENCY_KEY_INVALID_DETAIL,
        )
    return key


def _generation_response(view: GenerationView) -> GenerationResponse:
    generation = view.generation
    return GenerationResponse(
        id=generation.id,
        # The database constrains both columns to the documented sets.
        modality=cast(GenerationModality, generation.modality),
        model_id=generation.model_id,
        prompt=generation.prompt,
        params=dict(generation.params),
        status=cast(GenerationStatus, generation.status),
        progress=generation.progress,
        cost=generation.cost,
        provider=generation.provider,
        error_code=generation.error_code,
        assets=[
            GenerationAssetResponse(
                id=asset.id,
                url=f"/v1/assets/{asset.id}",
                mime_type=asset.mime_type,
                width=asset.width,
                height=asset.height,
                duration_ms=asset.duration_ms,
            )
            for asset in view.assets
        ],
        created_at=generation.created_at,
        started_at=generation.started_at,
        finished_at=generation.finished_at,
    )


@router.post(
    "/generations",
    response_model=GenerationResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Create a generation",
    description=(
        "Validates the request for the selected model, computes the server-owned "
        "cost, reserves Credits, and queues one Image or Video Generation. The "
        "`Idempotency-Key` header is required; replaying the same key and body "
        "returns the original Generation. Same key with a different body is "
        "`409 idempotency_conflict`."
    ),
    responses={
        409: {
            "description": "Insufficient Credits or an Idempotency-Key conflict.",
            **PROBLEM,
        },
        422: {
            "description": "The model is unavailable or the params are invalid.",
            **PROBLEM,
        },
    },
)
async def create_generation(
    body: CreateGenerationRequest,
    context: Annotated[AuthContext, Depends(get_current_auth)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> GenerationResponse:
    await enforce_rate_limit(
        limiter,
        f"generation-create:{context.user.id}",
        limit=GENERATIONS_PER_MINUTE,
        window_seconds=GENERATION_RATE_WINDOW_SECONDS,
    )
    view = await generation_service.create_generation(
        session_factory,
        user_id=context.user.id,
        idempotency_key=_idempotency_key(idempotency_key),
        modality=body.modality,
        model_id=body.model_id,
        prompt=body.prompt,
        params=body.params,
    )
    return _generation_response(view)


@router.get(
    "/generations",
    response_model=GenerationPage,
    summary="List generations",
    description=(
        "Returns the caller's Generations, newest first, as an opaque-cursor page. "
        "`status` filters before pagination; follow a cursor with the same filter."
    ),
)
async def list_generations(
    context: Annotated[AuthContext, Depends(get_current_auth)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    status_filter: Annotated[
        GenerationStatus | None,
        Query(alias="status", description="queued, running, succeeded, failed, or cancelled."),
    ] = None,
    cursor: Annotated[str | None, Query(description="Opaque cursor from `nextCursor`.")] = None,
    limit: Annotated[
        int,
        Query(
            ge=1,
            le=generation_service.GENERATION_MAX_PAGE_SIZE,
            description="Items per page.",
        ),
    ] = generation_service.GENERATION_PAGE_SIZE,
) -> GenerationPage:
    views, next_cursor = await generation_service.list_generations(
        session_factory,
        user_id=context.user.id,
        status=status_filter,
        cursor=cursor,
        limit=limit,
    )
    return GenerationPage(
        items=[_generation_response(view) for view in views],
        next_cursor=next_cursor,
    )


@router.get(
    "/generations/{generation_id}",
    response_model=GenerationResponse,
    summary="Get a generation",
    description="Returns one Generation owned by the caller, or 404.",
    responses={404: GENERATION_NOT_FOUND},
)
async def get_generation(
    generation_id: UUID,
    context: Annotated[AuthContext, Depends(get_current_auth)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> GenerationResponse:
    view = await generation_service.get_generation(
        session_factory,
        user_id=context.user.id,
        generation_id=generation_id,
    )
    return _generation_response(view)


@router.post(
    "/generations/{generation_id}/cancel",
    response_model=GenerationResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Cancel a generation",
    description=(
        "Cancels a queued or running Generation and refunds its reservation once. "
        "Cancelling an already-cancelled Generation returns it unchanged; a "
        "Generation that already succeeded or failed is `409 "
        "generation_not_cancellable`."
    ),
    responses={
        404: GENERATION_NOT_FOUND,
        409: {
            "description": "The Generation has already finished.",
            **PROBLEM,
        },
    },
)
async def cancel_generation(
    generation_id: UUID,
    context: Annotated[AuthContext, Depends(get_current_auth)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> GenerationResponse:
    view = await generation_service.cancel_generation(
        session_factory,
        user_id=context.user.id,
        generation_id=generation_id,
    )
    return _generation_response(view)
