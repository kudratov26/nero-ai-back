"""Readiness helper tests with a stubbed engine."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.exc import SQLAlchemyError
from starlette.requests import Request

from app.api.health import database_is_ready, expected_revision, get_engine


class _Result:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


class _Connection:
    def __init__(self, value: Any) -> None:
        self._value = value

    async def __aenter__(self) -> _Connection:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def execute(self, statement: object) -> _Result:
        return _Result(self._value)


class _Engine:
    def __init__(self, value: Any = None, error: Exception | None = None) -> None:
        self._value = value
        self._error = error

    def connect(self) -> _Connection:
        if self._error is not None:
            raise self._error
        return _Connection(self._value)


def test_expected_revision_matches_head() -> None:
    assert expected_revision() is not None


async def test_ready_when_revision_matches() -> None:
    assert await database_is_ready(_Engine(expected_revision())) is True  # type: ignore[arg-type]


async def test_not_ready_when_revision_is_stale() -> None:
    assert await database_is_ready(_Engine("stale-revision")) is False  # type: ignore[arg-type]


async def test_not_ready_when_connection_fails() -> None:
    engine = _Engine(error=SQLAlchemyError("connection refused"))

    assert await database_is_ready(engine) is False  # type: ignore[arg-type]


async def test_not_ready_when_driver_raises_os_error() -> None:
    engine = _Engine(error=ConnectionRefusedError())

    assert await database_is_ready(engine) is False  # type: ignore[arg-type]


def test_get_engine_requires_initialized_app() -> None:
    scope: dict[str, Any] = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/health/ready",
        "raw_path": b"/health/ready",
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "server": ("test", 80),
        "client": ("test", 1),
        "app": SimpleNamespace(state=SimpleNamespace()),
    }

    with pytest.raises(RuntimeError):
        get_engine(Request(scope))


async def test_revision_check_is_skipped_outside_a_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.setattr("app.core.readiness._REPO_ROOT", tmp_path)
    expected_revision.cache_clear()
    try:
        assert expected_revision() is None
        assert await database_is_ready(_Engine(None)) is True  # type: ignore[arg-type]
    finally:
        expected_revision.cache_clear()
