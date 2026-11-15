"""Public catalog endpoints: plans and models.

Both are anonymous reads. Nothing here is user-scoped, so there is no session
dependency; the route matrix classifies them as public.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_session_factory
from app.api.problems import PROBLEM_MEDIA_TYPE, ProblemDetail
from app.api.schemas.catalog import Modality, ModelResponse, PlanResponse
from app.db.models import Model, Plan
from app.domain import catalog as catalog_service

router = APIRouter(prefix="/v1", tags=["catalog"])


def _plan_response(plan: Plan) -> PlanResponse:
    return PlanResponse(
        id=plan.id,
        name=plan.name,
        price_cents=plan.price_cents,
        monthly_credits=plan.monthly_credits,
        tagline=plan.tagline,
        features=list(plan.features),
        highlighted=plan.highlighted,
        sort_order=plan.sort_order,
    )


def _model_response(model: Model) -> ModelResponse:
    return ModelResponse(
        id=model.id,
        name=model.name,
        provider=model.provider,
        modality=model.modality,
        description=model.description,
        badge=model.badge,
        credit_cost=model.credit_cost,
        credit_cost_10s=model.credit_cost_10s,
        capabilities=dict(model.capabilities),
        context_window=model.context_window,
        sort_order=model.sort_order,
    )


@router.get(
    "/plans",
    response_model=list[PlanResponse],
    summary="List plans",
    description="Returns the seeded plans in display order. Public.",
)
async def plans(
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> list[PlanResponse]:
    rows = await catalog_service.list_plans(session_factory)
    return [_plan_response(row) for row in rows]


@router.get(
    "/models",
    response_model=list[ModelResponse],
    summary="List models",
    description=(
        "Returns available models in display order, optionally filtered by modality. "
        "Models whose provider is not configured are never listed. Public."
    ),
)
async def models(
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    modality: Annotated[Modality | None, Query(description="chat, image, or video")] = None,
) -> list[ModelResponse]:
    rows = await catalog_service.list_models(session_factory, modality=modality)
    return [_model_response(row) for row in rows]


@router.get(
    "/models/{model_id}",
    response_model=ModelResponse,
    summary="Get a model",
    description="Returns one available model, or 404 when it is unknown or unavailable. Public.",
    responses={
        404: {
            "description": "The model is unknown or not available.",
            "content": {PROBLEM_MEDIA_TYPE: {"schema": ProblemDetail.model_json_schema()}},
        }
    },
)
async def model(
    model_id: str,
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> ModelResponse:
    row = await catalog_service.get_model(session_factory, model_id=model_id)
    return _model_response(row)
