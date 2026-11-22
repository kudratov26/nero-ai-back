"""Health probes."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncEngine

from app.api.problems import (
    PROBLEM_MEDIA_TYPE,
    ErrorCode,
    ProblemDetail,
    get_request_id,
    problem_response,
)
from app.core.readiness import database_is_ready, expected_revision

__all__ = ["database_is_ready", "expected_revision", "router"]

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


class ReadyResponse(BaseModel):
    status: Literal["ready"] = "ready"


def get_engine(request: Request) -> AsyncEngine:
    engine: AsyncEngine | None = getattr(request.app.state, "engine", None)
    if engine is None:
        raise RuntimeError("database engine is not initialized")
    return engine


@router.get(
    "/health/live",
    response_model=HealthResponse,
    summary="Liveness probe",
    description="Returns 200 while the process is alive. Does not touch dependencies.",
)
async def live() -> HealthResponse:
    return HealthResponse()


@router.get(
    "/health/ready",
    response_model=ReadyResponse,
    summary="Readiness probe",
    description="Returns 200 when the database is reachable and migrations are at head.",
    responses={
        503: {
            "description": "The database is unreachable or migrations are pending.",
            "content": {PROBLEM_MEDIA_TYPE: {"schema": ProblemDetail.model_json_schema()}},
        }
    },
)
async def ready(request: Request, engine: Annotated[AsyncEngine, Depends(get_engine)]) -> Response:
    if await database_is_ready(engine):
        return JSONResponse({"status": "ready"})
    return problem_response(
        status=503,
        code=ErrorCode.INTERNAL_ERROR,
        detail="The service is not ready.",
        request_id=get_request_id(request),
    )
