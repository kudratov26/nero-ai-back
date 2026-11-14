"""Health probes."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.api.problems import (
    PROBLEM_MEDIA_TYPE,
    ErrorCode,
    ProblemDetail,
    get_request_id,
    problem_response,
)

router = APIRouter(tags=["health"])

_REPO_ROOT = Path(__file__).resolve().parents[2]


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


class ReadyResponse(BaseModel):
    status: Literal["ready"] = "ready"


def get_engine(request: Request) -> AsyncEngine:
    engine: AsyncEngine | None = getattr(request.app.state, "engine", None)
    if engine is None:
        raise RuntimeError("database engine is not initialized")
    return engine


@lru_cache(maxsize=1)
def expected_revision() -> str | None:
    """Return the Alembic head, or None when this is not a source checkout."""
    config_path = _REPO_ROOT / "alembic.ini"
    if not config_path.exists():
        return None
    script = ScriptDirectory.from_config(Config(str(config_path)))
    return script.get_current_head()


async def database_is_ready(engine: AsyncEngine) -> bool:
    """Cheap readiness probe: connectivity plus migrations at head.

    Never surfaces driver messages or connection strings.
    """
    try:
        async with engine.connect() as connection:
            result = await connection.execute(text("SELECT version_num FROM alembic_version"))
            current = result.scalar_one_or_none()
    except (SQLAlchemyError, OSError):
        return False
    expected = expected_revision()
    if expected is None:
        return True
    return current == expected


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
