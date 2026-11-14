"""Auth helpers, password policy, and cookie attributes."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from starlette.requests import Request
from starlette.responses import Response

from app.api.auth import clear_session_cookie, set_session_cookie
from app.api.deps import get_session_factory
from app.api.problems import ApiError, ErrorCode
from app.domain.auth import (
    display_name_for,
    normalize_email,
    validate_password,
)
from tests.support import make_settings

PASSWORD = "correct horse battery"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Creator@Example.com", "creator@example.com"),
        ("  MIXED@EXAMPLE.COM  ", "mixed@example.com"),
        ("already@example.com", "already@example.com"),
    ],
)
def test_normalize_email(raw: str, expected: str) -> None:
    assert normalize_email(raw) == expected


@pytest.mark.parametrize(
    ("submitted", "email", "expected"),
    [
        ("Creator", "creator@example.com", "Creator"),
        ("  Creator  ", "creator@example.com", "Creator"),
        (None, "creator@example.com", "creator"),
        ("", "local.part@example.com", "local.part"),
        ("   ", "local@example.com", "local"),
    ],
)
def test_display_name_for(submitted: str | None, email: str, expected: str) -> None:
    assert display_name_for(submitted, email) == expected


def test_validate_password_accepts_a_reasonable_password() -> None:
    validate_password(PASSWORD, "creator@example.com")


@pytest.mark.parametrize(
    "password",
    [
        "short",
        "x" * 129,
        "creator@example.com",
        "Creator@Example.com",
        "password123",
    ],
)
def test_validate_password_rejects_weak_passwords(password: str) -> None:
    with pytest.raises(ApiError) as caught:
        validate_password(password, "creator@example.com")

    assert caught.value.code is ErrorCode.WEAK_PASSWORD
    assert caught.value.status == 422


def test_session_cookie_flags() -> None:
    response = Response()
    settings = make_settings()
    set_session_cookie(response, settings, "opaque-token")

    header = response.headers["set-cookie"]
    lowered = header.lower()
    assert header.startswith("nero_session=opaque-token")
    assert "httponly" in lowered
    assert "samesite=lax" in lowered
    assert "path=/" in lowered
    assert f"max-age={30 * 24 * 60 * 60}" in lowered
    assert "secure" not in lowered


def test_session_cookie_is_secure_when_configured() -> None:
    response = Response()
    settings = make_settings(cookie_secure=True)

    set_session_cookie(response, settings, "opaque-token")

    assert "secure" in response.headers["set-cookie"].lower()


def test_clear_session_cookie_expires_it() -> None:
    response = Response()
    settings = make_settings()

    clear_session_cookie(response, settings)

    header = response.headers["set-cookie"]
    lowered = header.lower()
    assert header.startswith('nero_session="";')
    assert "max-age=0" in lowered
    assert "httponly" in lowered


def test_session_factory_requires_an_initialized_app() -> None:
    scope: dict[str, Any] = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/v1/auth/me",
        "raw_path": b"/v1/auth/me",
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "server": ("test", 80),
        "client": ("test", 1),
        "app": SimpleNamespace(state=SimpleNamespace()),
    }

    with pytest.raises(RuntimeError):
        get_session_factory(Request(scope))
