"""Time-sortable UUIDv7 identifiers.

UUIDv7 puts a 48-bit Unix millisecond timestamp first, so IDs sort by creation
time and index well. Used for every persisted primary key from Phase 1 onward.
"""

from __future__ import annotations

import secrets
import time
from uuid import UUID


def uuid7() -> UUID:
    """Return a new UUIDv7 (RFC 9562) using the current time."""
    timestamp_ms = time.time_ns() // 1_000_000
    raw = bytearray(secrets.token_bytes(16))
    raw[0:6] = timestamp_ms.to_bytes(6, "big")
    raw[6] = (raw[6] & 0x0F) | 0x70  # version 7
    raw[8] = (raw[8] & 0x3F) | 0x80  # RFC 4122 variant
    return UUID(bytes=bytes(raw))


def uuid7_timestamp_ms(value: UUID) -> int:
    """Return the embedded creation timestamp in milliseconds."""
    return int.from_bytes(value.bytes[0:6], "big")
