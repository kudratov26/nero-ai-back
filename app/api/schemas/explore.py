"""Response schemas for Explore.

A Post carries the prompt, Model, and params a client needs to remix it, plus the
presentation fields the gallery renders: the author, the asset URL, the derived
like count, and whether the caller has liked it.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from app.api.schemas.base import CamelModel, UtcDatetime
from app.api.schemas.catalog import Modality


class PostAuthorResponse(CamelModel):
    id: UUID
    display_name: str
    initials: str


class PostAssetResponse(CamelModel):
    id: UUID
    #: Relative so the browser uses the same origin as the API (and the frontend
    #: proxy in production) instead of hard-coding a host.
    url: str
    mime_type: str
    width: int | None
    height: int | None
    duration_ms: int | None


class PostResponse(CamelModel):
    id: UUID
    modality: Modality
    title: str
    prompt: str
    model_id: str
    params: dict[str, Any]
    author: PostAuthorResponse | None
    asset: PostAssetResponse | None
    like_count: int
    liked_by_me: bool
    created_at: UtcDatetime


class PostPage(CamelModel):
    items: list[PostResponse]
    next_cursor: str | None
