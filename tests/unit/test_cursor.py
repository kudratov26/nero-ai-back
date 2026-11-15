"""Unit tests for opaque cursor encoding and the keyset helpers."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.core.cursor import (
    InvalidCursorError,
    decode_cursor,
    decode_keyset,
    encode_cursor,
    encode_keyset,
    isoformat_utc,
)


def test_round_trips_a_key() -> None:
    key = {"createdAt": "2026-11-15T12:00:00Z", "id": "0193a0e0-0000-7000-8000-000000000000"}

    assert decode_cursor(encode_cursor(key)) == key


def test_encoding_is_deterministic_and_url_safe() -> None:
    token = encode_cursor({"b": "2", "a": "1"})

    # Sorted keys make the token stable regardless of dict order.
    assert token == encode_cursor({"a": "1", "b": "2"})
    assert set(token) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_=")


def test_decoding_is_not_plainly_readable() -> None:
    token = encode_cursor({"id": "secret-looking-id"})

    assert "secret-looking-id" not in token


@pytest.mark.parametrize(
    "token",
    [
        "",
        "not base64 !!",
        base64.urlsafe_b64encode(b"[1, 2, 3]").decode(),  # a JSON list, not object
        base64.urlsafe_b64encode(b"plain text").decode(),
    ],
)
def test_a_malformed_cursor_is_rejected(token: str) -> None:
    with pytest.raises(InvalidCursorError):
        decode_cursor(token)


def test_timestamps_serialize_as_utc_with_z() -> None:
    moment = datetime(2026, 11, 15, 12, 30, tzinfo=timezone(timedelta(hours=2)))

    assert isoformat_utc(moment) == "2026-11-15T10:30:00Z"


def test_a_keyset_round_trips() -> None:
    moment = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
    resource_id = uuid4()

    timestamp, decoded_id = decode_keyset(encode_keyset(moment, resource_id))

    assert timestamp == moment
    assert timestamp.tzinfo is not None
    assert decoded_id == resource_id


def test_a_keyset_preserves_microseconds() -> None:
    moment = datetime(2026, 11, 15, 12, 0, 0, 123456, tzinfo=UTC)

    timestamp, _ = decode_keyset(encode_keyset(moment, uuid4()))

    # Truncating here would make the "after this row" comparison skip or repeat rows.
    assert timestamp == moment


@pytest.mark.parametrize(
    "payload",
    [
        {},  # missing both keys
        {"at": "2026-11-15T12:00:00Z"},  # missing id
        {"at": "not-a-date", "id": "0193a0e0-0000-7000-8000-000000000000"},
        {"at": "2026-11-15T12:00:00Z", "id": "not-a-uuid"},
        {"at": "2026-11-15T12:00:00", "id": "0193a0e0-0000-7000-8000-000000000000"},
    ],
)
def test_a_bad_keyset_is_rejected(payload: dict[str, str]) -> None:
    with pytest.raises(InvalidCursorError):
        decode_keyset(encode_cursor(payload))


def test_a_non_object_cursor_is_rejected_by_the_keyset_decoder() -> None:
    token = base64.urlsafe_b64encode(b'"a string"').decode()

    with pytest.raises(InvalidCursorError):
        decode_keyset(token)
