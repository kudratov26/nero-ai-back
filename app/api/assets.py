"""Asset delivery.

One route, and it is a binary response rather than JSON:

    GET /v1/assets/{id}

`FileResponse` supplies `ETag`, `Last-Modified`, `Accept-Ranges`, conditional
requests, and HTTP Range handling, including `416` for an unsatisfiable range. The
route never mounts the storage directory, so the only way to reach a file is an
`assets` row plus a permission check.
"""

from __future__ import annotations

from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_optional_auth, get_session_factory
from app.api.problems import PROBLEM_MEDIA_TYPE, ProblemDetail
from app.domain import assets as asset_service
from app.domain.auth import AuthContext
from app.storage.local import LocalStorage

router = APIRouter(prefix="/v1", tags=["assets"])

ASSET_DESCRIPTION = (
    "Returns the bytes for one asset. The owner may always read it and an asset on a "
    "published Explore post is public; anyone else gets 404, exactly as for an unknown "
    "id. Supports conditional and Range requests."
)


def get_storage(request: Request) -> LocalStorage:
    """Return the app-wide storage adapter built by the lifespan."""
    storage = getattr(request.app.state, "storage", None)
    if storage is None:
        raise RuntimeError("the storage adapter is not initialized")
    return cast(LocalStorage, storage)


@router.get(
    "/assets/{asset_id}",
    summary="Download an asset",
    description=ASSET_DESCRIPTION,
    response_class=FileResponse,
    responses={
        200: {
            "description": "The asset bytes.",
            "content": {"application/octet-stream": {"schema": {"type": "string"}}},
        },
        206: {"description": "A byte range of the asset."},
        404: {
            "description": "The asset does not exist or is not readable by the caller.",
            "content": {PROBLEM_MEDIA_TYPE: {"schema": ProblemDetail.model_json_schema()}},
        },
    },
)
async def download_asset(
    asset_id: UUID,
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    auth: Annotated[AuthContext | None, Depends(get_optional_auth)],
    storage: Annotated[LocalStorage, Depends(get_storage)],
) -> FileResponse:
    found = await asset_service.load_for_read(
        session_factory,
        storage,
        asset_id=asset_id,
        user_id=None if auth is None else auth.user.id,
    )
    # `FileResponse` stats the path itself, so `Content-Length`, `ETag`, and
    # `Last-Modified` describe the bytes actually on disk.
    return FileResponse(
        path=found.file.path,
        media_type=found.asset.mime_type,
        headers={
            "Cache-Control": found.cache_control,
            # An asset id is a capability-ish URL even when private, so a browser
            # must not re-interpret the bytes as another type.
            "X-Content-Type-Options": "nosniff",
        },
    )
