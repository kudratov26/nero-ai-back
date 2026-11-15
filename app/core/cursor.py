"""Opaque keyset cursors.

A cursor encodes the sort key of the last item on a page as base64url of a compact
JSON object, so it is opaque to clients and can carry whatever key each endpoint
sorts by. Clients must treat it as an opaque token; a malformed one is a `400`.
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any


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
