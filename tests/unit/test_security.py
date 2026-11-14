"""Password hashing and session token primitives."""

from __future__ import annotations

import re

from app.core.security import (
    SESSION_TOKEN_BYTES,
    generate_session_token,
    hash_password,
    hash_session_token,
    verify_password,
)

URLSAFE_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def test_hash_password_uses_argon2id() -> None:
    hashed = hash_password("correct horse battery staple")

    assert hashed.startswith("$argon2id$")
    assert "correct horse battery staple" not in hashed


def test_hash_password_salts_each_hash() -> None:
    first = hash_password("correct horse battery staple")
    second = hash_password("correct horse battery staple")

    assert first != second


def test_verify_password_accepts_matching_password() -> None:
    hashed = hash_password("correct horse battery staple")

    assert verify_password("correct horse battery staple", hashed) is True


def test_verify_password_rejects_wrong_password() -> None:
    hashed = hash_password("correct horse battery staple")

    assert verify_password("wrong horse battery staple", hashed) is False


def test_verify_password_treats_unknown_hash_as_failure() -> None:
    assert verify_password("anything", "not-a-password-hash") is False


def test_session_token_is_urlsafe_and_has_256_bits() -> None:
    token = generate_session_token()

    assert URLSAFE_PATTERN.fullmatch(token)
    assert len(token) >= 43
    assert SESSION_TOKEN_BYTES == 32


def test_session_tokens_are_unique() -> None:
    assert len({generate_session_token() for _ in range(200)}) == 200


def test_hash_session_token_is_sha256() -> None:
    token = generate_session_token()

    digest = hash_session_token(token)

    assert isinstance(digest, bytes)
    assert len(digest) == 32
    assert digest == hash_session_token(token)


def test_hash_session_token_differs_per_token() -> None:
    assert hash_session_token("one-token") != hash_session_token("another-token")
