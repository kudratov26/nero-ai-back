"""Prometheus scrape endpoint.

The endpoint is intentionally public: a collector usually runs on the same
private network as the API. Restrict `/metrics` at the reverse proxy when the API
is exposed to the internet. It exposes counters and durations only, never user
ids, prompts, tokens, or provider bodies.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from app.core.metrics import METRICS

router = APIRouter(tags=["operations"])

PROMETHEUS_MEDIA_TYPE = "text/plain; version=0.0.4; charset=utf-8"


@router.get(
    "/metrics",
    response_class=PlainTextResponse,
    summary="Scrape metrics",
    description=(
        "Returns process metrics in the Prometheus text exposition format. "
        "Restrict this path at the reverse proxy if the API is public."
    ),
)
async def metrics() -> PlainTextResponse:
    return PlainTextResponse(
        METRICS.render(),
        media_type=PROMETHEUS_MEDIA_TYPE,
        headers={"Cache-Control": "no-store"},
    )
