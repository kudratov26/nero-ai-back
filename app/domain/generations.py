"""Generation use cases: create, list, read, and cancel.

The server owns the price. `create_generation` loads the Model from the catalog,
validates the modality-specific params against the capabilities that model
advertises, computes the cost, inserts the queued row, and reserves the same
amount from the Credit account — all in one transaction. A request that fails
validation or cannot be covered leaves nothing behind.

Every read is scoped by `user_id`, so another account's Generation is reported
as `404 not_found`, exactly like one that does not exist. The create endpoint is
idempotent: `Idempotency-Key` names the operation, and a replay returns the
Generation the first request created instead of reserving Credits twice.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.problems import ApiError, ErrorCode
from app.core.cursor import InvalidCursorError, decode_keyset, encode_keyset
from app.db.models import Asset, Generation, Model
from app.db.models.common import utcnow
from app.db.models.generation import MAX_PROMPT_LENGTH
from app.db.session import session_scope
from app.domain import credits, idempotency

GenerationModality = Literal["image", "video"]
GenerationStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]

GENERATION_PAGE_SIZE = 20
GENERATION_MAX_PAGE_SIZE = 100

MODEL_UNAVAILABLE_DETAIL = "That model is not available."
INVALID_PARAMS_DETAIL = "Those params are not valid for this model."
INVALID_PROMPT_DETAIL = (
    f"The prompt must be between 1 and {MAX_PROMPT_LENGTH} characters after trimming."
)
GENERATION_NOT_FOUND_DETAIL = "That generation does not exist."
GENERATION_NOT_CANCELLABLE_DETAIL = "This generation has already finished."
INVALID_CURSOR_DETAIL = "The cursor is not valid."

IMAGE_CREDIT_REASON = "Image generation"
VIDEO_CREDIT_REASON = "Video generation"
CANCEL_CREDIT_REASON = "Cancelled generation"

#: Fallbacks for a catalog row that omits a capability. The seed advertises the
#: same values, so the client and the validator agree in a normal deployment.
IMAGE_ASPECT_RATIOS = ("1:1", "4:5", "16:9", "9:16")
VIDEO_ASPECT_RATIOS = ("16:9", "9:16", "1:1")
IMAGE_STYLES = ("None", "Photographic", "Cinematic", "Illustration", "Anime", "3D")
IMAGE_MAX_COUNT = 4
VIDEO_DURATIONS = (5, 10)

DEFAULT_IMAGE_ASPECT_RATIO = "1:1"
DEFAULT_IMAGE_COUNT = 1
DEFAULT_IMAGE_STYLE = "None"
DEFAULT_VIDEO_ASPECT_RATIO = "16:9"
DEFAULT_VIDEO_DURATION = 5

_IMAGE_KEYS = frozenset({"aspectRatio", "count", "style"})
_VIDEO_KEYS = frozenset({"aspectRatio", "duration"})


@dataclass(frozen=True)
class GenerationView:
    """A Generation and its output assets, read in one transaction."""

    generation: Generation
    assets: tuple[Asset, ...]


def _invalid_params() -> ApiError:
    # The detail is deliberately generic: never echo a value the client sent.
    return ApiError(ErrorCode.INVALID_REQUEST, 422, detail=INVALID_PARAMS_DETAIL)


def _prompt_error() -> ApiError:
    return ApiError(ErrorCode.INVALID_REQUEST, 422, detail=INVALID_PROMPT_DETAIL)


def normalize_prompt(value: str) -> str:
    """Trim and enforce the documented prompt bounds."""
    trimmed = value.strip()
    if not trimmed or len(trimmed) > MAX_PROMPT_LENGTH:
        raise _prompt_error()
    return trimmed


def _capability_strings(
    capabilities: Mapping[str, Any],
    key: str,
    fallback: tuple[str, ...],
) -> tuple[str, ...]:
    """Return an advertised string list, or the documented fallback."""
    value = capabilities.get(key)
    if isinstance(value, list) and value and all(isinstance(item, str) and item for item in value):
        return tuple(value)
    return fallback


def _capability_durations(capabilities: Mapping[str, Any]) -> tuple[int, ...]:
    value = capabilities.get("durations")
    if (
        isinstance(value, list)
        and value
        and all(isinstance(item, int) and not isinstance(item, bool) and item > 0 for item in value)
    ):
        return tuple(value)
    return VIDEO_DURATIONS


def _max_count(capabilities: Mapping[str, Any]) -> int:
    value = capabilities.get("maxCount")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return IMAGE_MAX_COUNT


def _default_from(values: tuple[str, ...], preferred: str) -> str:
    return preferred if preferred in values else values[0]


def _validate_image_params(
    params: Mapping[str, Any],
    capabilities: Mapping[str, Any],
) -> dict[str, Any]:
    if set(params) - _IMAGE_KEYS:
        raise _invalid_params()

    aspect_ratios = _capability_strings(
        capabilities,
        "aspectRatios",
        IMAGE_ASPECT_RATIOS,
    )
    styles = _capability_strings(capabilities, "styles", IMAGE_STYLES)
    maximum = _max_count(capabilities)

    aspect_ratio = params.get(
        "aspectRatio", _default_from(aspect_ratios, DEFAULT_IMAGE_ASPECT_RATIO)
    )
    if not isinstance(aspect_ratio, str) or aspect_ratio not in aspect_ratios:
        raise _invalid_params()

    count = params.get("count", DEFAULT_IMAGE_COUNT)
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= maximum:
        raise _invalid_params()

    style = params.get("style", _default_from(styles, DEFAULT_IMAGE_STYLE))
    if not isinstance(style, str) or style not in styles:
        raise _invalid_params()

    return {"aspectRatio": aspect_ratio, "count": count, "style": style}


def _validate_video_params(
    params: Mapping[str, Any],
    capabilities: Mapping[str, Any],
) -> dict[str, Any]:
    if set(params) - _VIDEO_KEYS:
        raise _invalid_params()

    aspect_ratios = _capability_strings(
        capabilities,
        "aspectRatios",
        VIDEO_ASPECT_RATIOS,
    )
    durations = _capability_durations(capabilities)

    aspect_ratio = params.get(
        "aspectRatio", _default_from(aspect_ratios, DEFAULT_VIDEO_ASPECT_RATIO)
    )
    if not isinstance(aspect_ratio, str) or aspect_ratio not in aspect_ratios:
        raise _invalid_params()

    duration = params.get(
        "duration",
        DEFAULT_VIDEO_DURATION if DEFAULT_VIDEO_DURATION in durations else durations[0],
    )
    if isinstance(duration, bool) or not isinstance(duration, int) or duration not in durations:
        raise _invalid_params()

    return {"aspectRatio": aspect_ratio, "duration": duration}


def validate_params(
    modality: str,
    params: Mapping[str, Any],
    capabilities: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate params for one modality and return the canonical stored shape.

    Advertised capabilities win over the fallback lists. Unknown keys are
    rejected consistently, so a typo cannot be silently ignored and then fail in
    the worker.
    """
    if modality == "image":
        return _validate_image_params(params, capabilities)
    if modality == "video":
        return _validate_video_params(params, capabilities)
    raise _invalid_params()


def cost_for(model: Model, *, params: Mapping[str, Any]) -> int:
    """Return the server-owned cost for already-validated params.

    Image cost is `creditCost * count`. A 5-second video uses `creditCost`; a
    10-second video uses `creditCost10s` when the catalog sets one.
    """
    if model.modality == "image":
        count = params.get("count")
        if isinstance(count, bool) or not isinstance(count, int):
            raise ValueError("image params were not validated")
        return model.credit_cost * count
    if model.modality == "video":
        duration = params.get("duration")
        if isinstance(duration, bool) or not isinstance(duration, int):
            raise ValueError("video params were not validated")
        if duration == 10 and model.credit_cost_10s is not None:
            return model.credit_cost_10s
        return model.credit_cost
    raise ValueError(f"a {model.modality!r} model cannot make a Generation")


def credit_reason(modality: str) -> str:
    return IMAGE_CREDIT_REASON if modality == "image" else VIDEO_CREDIT_REASON


async def _require_model(
    session: AsyncSession,
    *,
    model_id: str,
    modality: str,
) -> Model:
    """Return a servable model for this modality.

    Unknown, unavailable, and wrong-modality models get the same answer on
    purpose: the catalog is the only place that says what can run.
    """
    model = await session.get(Model, model_id)
    if model is None or model.modality != modality or not model.available:
        raise ApiError(ErrorCode.MODEL_UNAVAILABLE, 422, detail=MODEL_UNAVAILABLE_DETAIL)
    return model


async def _create_in_session(
    session: AsyncSession,
    *,
    user_id: UUID,
    modality: GenerationModality,
    model_id: str,
    prompt: str,
    params: Mapping[str, Any],
) -> UUID:
    """Insert one queued Generation and reserve its cost.

    Runs in the caller's transaction, which may also hold the idempotency record.
    """
    model = await _require_model(session, model_id=model_id, modality=modality)
    canonical_prompt = normalize_prompt(prompt)
    canonical_params = validate_params(modality, params, model.capabilities or {})
    cost = cost_for(model, params=canonical_params)
    if cost < 0:
        raise ValueError("a Generation cost cannot be negative")

    generation = Generation(
        user_id=user_id,
        modality=modality,
        model_id=model.id,
        prompt=canonical_prompt,
        params=canonical_params,
        status="queued",
        progress=0,
        cost=cost,
        provider=model.provider,
        attempts=0,
    )
    session.add(generation)
    await session.flush()

    if cost > 0:
        await credits.reserve(
            session,
            user_id=user_id,
            amount=cost,
            reason=credit_reason(modality),
            ref_type="generation",
            ref_id=generation.id,
        )
    return generation.id


async def create_generation(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: UUID,
    idempotency_key: str,
    modality: GenerationModality,
    model_id: str,
    prompt: str,
    params: Mapping[str, Any],
) -> GenerationView:
    """Create a Generation once for `(user_id, idempotency_key)`.

    The request hash covers the validated prompt and the raw params, so the same
    key with a different body is a `409 idempotency_conflict` rather than a
    second charge.
    """
    canonical_prompt = normalize_prompt(prompt)
    request_hash = idempotency.hash_request(
        {
            "modality": modality,
            "model_id": model_id,
            "prompt": canonical_prompt,
            "params": dict(params),
        }
    )

    async def create(session: AsyncSession) -> UUID:
        return await _create_in_session(
            session,
            user_id=user_id,
            modality=modality,
            model_id=model_id,
            prompt=canonical_prompt,
            params=params,
        )

    outcome = await idempotency.execute(
        session_factory,
        user_id=user_id,
        key=idempotency_key,
        operation=idempotency.GENERATION_CREATE,
        request_hash=request_hash,
        create=create,
    )
    return await get_generation(
        session_factory,
        user_id=user_id,
        generation_id=outcome.resource_id,
    )


async def _assets_for(
    session: AsyncSession,
    generation_ids: list[UUID],
) -> dict[UUID, list[Asset]]:
    if not generation_ids:
        return {}
    rows = await session.scalars(
        select(Asset)
        .where(Asset.generation_id.in_(generation_ids))
        .order_by(Asset.created_at, Asset.id)
    )
    grouped: dict[UUID, list[Asset]] = {}
    for asset in rows:
        if asset.generation_id is not None:
            grouped.setdefault(asset.generation_id, []).append(asset)
    return grouped


async def get_generation(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: UUID,
    generation_id: UUID,
) -> GenerationView:
    """Return one Generation owned by the caller, or `404 not_found`."""
    async with session_scope(session_factory) as session:
        generation = await session.scalar(
            select(Generation).where(
                Generation.id == generation_id,
                Generation.user_id == user_id,
            )
        )
        if generation is None:
            raise ApiError(ErrorCode.NOT_FOUND, 404, detail=GENERATION_NOT_FOUND_DETAIL)
        assets = await _assets_for(session, [generation.id])
        return GenerationView(generation, tuple(assets.get(generation.id, ())))


def _decode_generation_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        return decode_keyset(cursor)
    except InvalidCursorError as exc:
        raise ApiError(ErrorCode.INVALID_REQUEST, 400, detail=INVALID_CURSOR_DETAIL) from exc


async def list_generations(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: UUID,
    status: GenerationStatus | None,
    cursor: str | None,
    limit: int,
) -> tuple[list[GenerationView], str | None]:
    """Return one page of the caller's Generations, newest first."""
    statement = (
        select(Generation)
        .where(Generation.user_id == user_id)
        .order_by(Generation.created_at.desc(), Generation.id.desc())
        .limit(limit + 1)
    )
    if status is not None:
        statement = statement.where(Generation.status == status)
    if cursor is not None:
        after = _decode_generation_cursor(cursor)
        statement = statement.where(tuple_(Generation.created_at, Generation.id) < after)

    async with session_scope(session_factory) as session:
        rows = list(await session.scalars(statement))
        has_more = len(rows) > limit
        page = rows[:limit]
        assets = await _assets_for(session, [generation.id for generation in page])
        views = [
            GenerationView(generation, tuple(assets.get(generation.id, ()))) for generation in page
        ]

    next_cursor = encode_keyset(page[-1].created_at, page[-1].id) if has_more and page else None
    return views, next_cursor


async def cancel_generation(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: UUID,
    generation_id: UUID,
) -> GenerationView:
    """Cancel a queued or running Generation and refund its reservation once.

    A repeated cancel of an already-cancelled row is a no-op, so a retry is safe.
    A Generation that already succeeded or failed is `409
    generation_not_cancellable`. The row lock makes exactly-once refunding true
    even under concurrent cancels.
    """
    async with session_scope(session_factory) as session:
        generation = await session.scalar(
            select(Generation)
            .where(
                Generation.id == generation_id,
                Generation.user_id == user_id,
            )
            .with_for_update()
        )
        if generation is None:
            raise ApiError(ErrorCode.NOT_FOUND, 404, detail=GENERATION_NOT_FOUND_DETAIL)

        if generation.status in ("succeeded", "failed"):
            raise ApiError(
                ErrorCode.GENERATION_NOT_CANCELLABLE,
                409,
                detail=GENERATION_NOT_CANCELLABLE_DETAIL,
            )

        if generation.status != "cancelled":
            generation.status = "cancelled"
            generation.progress = 0
            generation.finished_at = utcnow()
            await session.flush()
            if generation.cost > 0:
                await credits.refund(
                    session,
                    user_id=user_id,
                    amount=generation.cost,
                    reason=CANCEL_CREDIT_REASON,
                    ref_type="generation",
                    ref_id=generation.id,
                )

        assets = await _assets_for(session, [generation.id])
        return GenerationView(generation, tuple(assets.get(generation.id, ())))
