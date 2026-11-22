"""Asset delivery.

One route, and it is a binary response rather than JSON:

    GET /v1/assets/{id}

`FileResponse` supplies `ETag`, `Last-Modified`, `Accept-Ranges`, conditional
requests, and HTTP Range handling, including `416` for an unsatisfiable range. The
route never mounts the storage directory, so the only way to reach a file is an
`assets` row plus a permission check.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.asset_response import asset_response
from app.api.deps import get_optional_auth, get_session_factory, get_storage
from app.api.problems import PROBLEM_MEDIA_TYPE, ProblemDetail
from app.domain import assets as asset_service
from app.domain.auth import AuthContext
from app.storage.base import Storage

router = APIRouter(prefix="/v1", tags=["assets"])

ASSET_DESCRIPTION = (
    "Returns the bytes for one asset. The owner may always read it and an asset on a "
    "published Explore post is public; anyone else gets 404, exactly as for an unknown "
    "id. Supports conditional and Range requests."
)


@router.head("/assets/{asset_id}", include_in_schema=False)
@router.get(
    "/assets/{asset_id}",
    summary="Download an asset",
    description=ASSET_DESCRIPTION,
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "The asset bytes.",
            "content": {"application/octet-stream": {"schema": {"type": "string"}}},
        },
        206: {"description": "A byte range of the asset."},
        304: {"description": "The caller's cached copy is still current."},
        404: {
            "description": "The asset does not exist or is not readable by the caller.",
            "content": {PROBLEM_MEDIA_TYPE: {"schema": ProblemDetail.model_json_schema()}},
        },
    },
)
async def download_asset(
    asset_id: UUID,
    request: Request,
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    auth: Annotated[AuthContext | None, Depends(get_optional_auth)],
    storage: Annotated[Storage, Depends(get_storage)],
) -> Response:
    found = await asset_service.load_for_read(
        session_factory,
        storage,
        asset_id=asset_id,
        user_id=None if auth is None else auth.user.id,
    )
    return asset_response(
        request,
        storage,
        key=found.asset.storage_key,
        file=found.file,
        media_type=found.asset.mime_type,
        cache_control=found.cache_control,
    )
