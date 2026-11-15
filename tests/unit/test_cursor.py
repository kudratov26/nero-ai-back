"""Unit tests for opaque cursor encoding."""

from __future__ import annotations

import base64

import pytest

from app.core.cursor import InvalidCursorError, decode_cursor, encode_cursor


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
