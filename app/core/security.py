"""Password hashing and session token primitives.

Argon2id is the only accepted password hash. Session tokens are 32 random bytes
encoded URL-safe for the cookie; only their SHA-256 digest is persisted.
"""

from __future__ import annotations

import hashlib
import secrets

from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError

SESSION_TOKEN_BYTES = 32

_password_hash = PasswordHash.recommended()


def hash_password(password: str) -> str:
    """Return an Argon2id hash for `password`."""
    return _password_hash.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Check `password` against a stored hash.

    Unknown or malformed hashes are treated as a failed login rather than an
    error, so a corrupted row cannot produce a different response.
    """
    try:
        return _password_hash.verify(password, password_hash)
    except UnknownHashError:
        return False


def generate_session_token() -> str:
    """Return a new opaque session token, URL-safe encoded."""
    return secrets.token_urlsafe(SESSION_TOKEN_BYTES)


def hash_session_token(token: str) -> bytes:
    """Return the SHA-256 digest stored in `sessions.token_hash`."""
    return hashlib.sha256(token.encode("utf-8")).digest()
