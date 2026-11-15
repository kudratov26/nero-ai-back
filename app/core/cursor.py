"""Opaque keyset cursors.

A cursor encodes the sort key of the last item on a page as base64url of a compact
JSON object, so it is opaque to clients and can carry whatever key each endpoint
sorts by. Clients must treat it as an opaque token; a malformed one is a `400`.

Most pages sort newest-first by `(timestamp, id)`, so `encode_keyset` and
`decode_keyset` cover that shape; an endpoint that sorts differently can still use
`encode_cursor`/`decode_cursor` directly.
"""

from __future__ import annotations

import base64
import binascii
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

TIMESTAMP_KEY = "at"
ID_KEY = "id"
_MALFORMED_KEYSET = "cursor is not a keyset"


class InvalidCursorError(ValueError):
    """Raised when a cursor cannot be decoded."""


def encode_cursor(values: dict[str, Any]) -> str:
    """Encode a sort key into an opaque, URL-safe token."""
    payload = json.dumps(values, separators=(",", ":"), sort_keys=True)
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def decode_cursor(token: str) -> dict[str, Any]:
    """Decode a cursor back into its sort key, or raise `InvalidCursorError`."""
    padding = "=" * (-len(token) % 4)
    try:
        raw = base64.urlsafe_b64decode(token + padding)
        data = json.loads(raw)
    except (binascii.Error, ValueError) as exc:
        raise InvalidCursorError("cursor is not decodable") from exc
    if not isinstance(data, dict):
        raise InvalidCursorError("cursor is not a key object")
    return data


def isoformat_utc(value: datetime) -> str:
    """Serialize a timestamp the way the HTTP boundary does: ISO 8601 with `Z`."""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def encode_keyset(timestamp: datetime, resource_id: UUID) -> str:
    """Encode a `(timestamp, id)` sort key, the shape of a newest-first page."""
    return encode_cursor({TIMESTAMP_KEY: isoformat_utc(timestamp), ID_KEY: str(resource_id)})


def decode_keyset(token: str) -> tuple[datetime, UUID]:
    """Decode a `(timestamp, id)` sort key, or raise `InvalidCursorError`.

    A timezone-naive timestamp is rejected: every stored timestamp is UTC, and a
    naive one would silently compare in the wrong frame.
    """
    try:
        data = decode_cursor(token)
        timestamp = datetime.fromisoformat(str(data[TIMESTAMP_KEY]))
        resource_id = UUID(str(data[ID_KEY]))
    except (InvalidCursorError, KeyError, ValueError) as exc:
        raise InvalidCursorError(_MALFORMED_KEYSET) from exc
    if timestamp.tzinfo is None:
        raise InvalidCursorError(_MALFORMED_KEYSET)
    return timestamp, resource_id
