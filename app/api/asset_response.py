"""Turn one stored object into an HTTP response.

`FileResponse` cannot serve S3 objects, so this module implements the small part
of the delivery contract both backends share: `ETag`, `Last-Modified`,
`Cache-Control`, conditional `304`, single byte ranges, and `416` for a range
outside the object. Bytes are streamed through the storage port and never
materialized whole.

Only one range is honored. A multi-range request falls back to the full object,
which RFC 9110 allows: a server may ignore `Range`.
"""

from __future__ import annotations

from email.utils import formatdate, parsedate_to_datetime

from fastapi import Request
from starlette.responses import Response, StreamingResponse

from app.storage.base import Storage, StoredFile


class _MalformedRange(Exception):
    pass


class _RangeNotSatisfiable(Exception):
    pass


def _parse_range(value: str, size: int) -> tuple[int, int] | None:
    """Parse one byte range. `None` means "ignore the header and send it all"."""
    unit, separator, spec = value.partition("=")
    if not separator or unit.strip().lower() != "bytes":
        raise _MalformedRange
    spec = spec.strip()
    if not spec:
        raise _MalformedRange
    if "," in spec:
        return None
    if "-" not in spec:
        raise _MalformedRange

    start_text, end_text = (part.strip() for part in spec.split("-", 1))
    try:
        if start_text:
            start = int(start_text)
            end = int(end_text) + 1 if end_text else size
        else:
            if not end_text:
                raise _MalformedRange
            suffix = int(end_text)
            if suffix <= 0:
                raise _MalformedRange
            start, end = max(size - suffix, 0), size
    except ValueError:
        raise _MalformedRange from None

    if start >= size:
        raise _RangeNotSatisfiable
    if start < 0 or end <= start:
        raise _MalformedRange
    return start, min(end, size)


def _etag_matches(header: str, etag: str | None) -> bool:
    if etag is None:
        return False
    if header.strip() == "*":
        return True
    normalized = etag.strip()
    for candidate in header.split(","):
        value = candidate.strip()
        if value.startswith("W/"):
            value = value[2:]
        if value == normalized:
            return True
    return False


def _not_modified(request: Request, file: StoredFile, last_modified: str | None) -> bool:
    if_none_match = request.headers.get("if-none-match")
    if if_none_match is not None:
        return _etag_matches(if_none_match, file.etag)
    if_modified_since = request.headers.get("if-modified-since")
    if if_modified_since is None or last_modified is None:
        return False
    try:
        since = parsedate_to_datetime(if_modified_since)
    except (TypeError, ValueError):
        return False
    return int(file.mtime) <= int(since.timestamp())


def _if_range_matches(value: str, file: StoredFile, last_modified: str | None) -> bool:
    if value.startswith("W/"):
        return False
    return value == file.etag or (last_modified is not None and value == last_modified)


def asset_response(
    request: Request,
    storage: Storage,
    *,
    key: str,
    file: StoredFile,
    media_type: str,
    cache_control: str,
) -> Response:
    """Build the documented response for an already-authorized asset."""
    headers = {
        "Cache-Control": cache_control,
        "X-Content-Type-Options": "nosniff",
        "Accept-Ranges": "bytes",
    }
    if file.etag is not None:
        headers["ETag"] = file.etag
    last_modified = formatdate(file.mtime, usegmt=True) if file.mtime > 0 else None
    if last_modified is not None:
        headers["Last-Modified"] = last_modified

    method = request.method.upper()
    readable = method in {"GET", "HEAD"}

    if readable and _not_modified(request, file, last_modified):
        return Response(status_code=304, headers=headers)

    start = 0
    end = file.size_bytes
    status_code = 200
    range_header = request.headers.get("range")
    if_range = request.headers.get("if-range")
    if (
        readable
        and range_header is not None
        and (if_range is None or _if_range_matches(if_range, file, last_modified))
    ):
        try:
            parsed = _parse_range(range_header, file.size_bytes)
        except _MalformedRange:
            return Response("Invalid Range header", status_code=400)
        except _RangeNotSatisfiable:
            return Response(
                status_code=416,
                headers={
                    "Content-Range": f"bytes */{file.size_bytes}",
                    "Accept-Ranges": "bytes",
                },
            )
        if parsed is not None:
            start, end = parsed
            status_code = 206
            headers["Content-Range"] = f"bytes {start}-{end - 1}/{file.size_bytes}"
            headers["Content-Length"] = str(end - start)
    if "Content-Length" not in headers:
        headers["Content-Length"] = str(end - start)

    if method == "HEAD":
        return Response(status_code=status_code, headers=headers, media_type=media_type)
    return StreamingResponse(
        storage.iter_range(key, start=start, end=end),
        status_code=status_code,
        headers=headers,
        media_type=media_type,
    )
