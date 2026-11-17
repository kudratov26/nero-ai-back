"""Request, response, and page schemas for Generations.

`params` is a free-form object at the boundary because its keys depend on the
modality. The domain validates it against the selected model's advertised
capabilities and returns the canonical stored shape.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import Field, field_validator

from app.api.schemas.base import CamelModel, CamelRequest, UtcDatetime
from app.db.models.generation import MAX_PROMPT_LENGTH

GenerationModality = Literal["image", "video"]
GenerationStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]

MAX_MODEL_ID_LENGTH = 120


class CreateGenerationRequest(CamelRequest):
    modality: GenerationModality
    model_id: str = Field(min_length=1, max_length=MAX_MODEL_ID_LENGTH)
    prompt: str
    params: dict[str, Any] = Field(default_factory=dict)

    @field_validator("prompt")
    @classmethod
    def _validate_prompt(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("prompt cannot be blank")
        if len(trimmed) > MAX_PROMPT_LENGTH:
            raise ValueError(f"prompt must be at most {MAX_PROMPT_LENGTH} characters")
        return trimmed


class GenerationAssetResponse(CamelModel):
    id: UUID
    url: str
    mime_type: str
    width: int | None
    height: int | None
    duration_ms: int | None


class GenerationResponse(CamelModel):
    id: UUID
    modality: GenerationModality
    model_id: str
    prompt: str
    params: dict[str, Any]
    status: GenerationStatus
    progress: int
    #: Credits reserved when the Generation was created. Server-owned.
    cost: int
    provider: str
    error_code: str | None
    assets: list[GenerationAssetResponse]
    created_at: UtcDatetime
    started_at: UtcDatetime | None
    finished_at: UtcDatetime | None


class GenerationPage(CamelModel):
    items: list[GenerationResponse]
    next_cursor: str | None
