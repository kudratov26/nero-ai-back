"""Health probes."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


@router.get(
    "/health/live",
    response_model=HealthResponse,
    summary="Liveness probe",
    description="Returns 200 while the process is alive. Does not touch dependencies.",
)
async def live() -> HealthResponse:
    return HealthResponse()
