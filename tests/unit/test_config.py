"""Tests for environment-driven configuration."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import DEV_APP_SECRET, AppEnv, Settings
from tests.support import make_settings


def test_requires_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_development_defaults() -> None:
    settings = make_settings(app_env="development")
    assert settings.app_env is AppEnv.DEVELOPMENT
    assert settings.frontend_origin == "http://localhost:3000"
    assert settings.session_cookie_name == "nero_session"
    assert settings.session_ttl_days == 30
    assert settings.secure_cookies is False
    assert settings.is_production is False


def test_log_level_is_case_insensitive() -> None:
    assert make_settings(log_level="INFO").log_level == "info"


def test_invalid_log_level_rejected() -> None:
    with pytest.raises(ValidationError):
        make_settings(log_level="verbose")


def test_invalid_app_env_rejected() -> None:
    with pytest.raises(ValidationError):
        make_settings(app_env="staging")


def test_session_ttl_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        make_settings(session_ttl_days=0)


def test_production_requires_real_secret() -> None:
    with pytest.raises(ValidationError):
        make_settings(app_env="production", app_secret=DEV_APP_SECRET)


def test_production_rejects_insecure_cookie() -> None:
    with pytest.raises(ValidationError):
        make_settings(app_env="production", cookie_secure=False)


def test_production_defaults_to_secure_cookies() -> None:
    settings = make_settings(app_env="production", app_secret="a-real-production-secret")
    assert settings.is_production is True
    assert settings.secure_cookies is True


def test_cookie_secure_can_be_overridden_in_development() -> None:
    assert make_settings(cookie_secure=True).secure_cookies is True


def test_body_limit_defaults_to_one_mib() -> None:
    assert make_settings().max_request_body_bytes == 1_048_576


def test_body_limit_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        make_settings(max_request_body_bytes=0)


def test_memory_rate_limit_backend_is_the_default() -> None:
    assert make_settings().rate_limit_backend == "memory"


def test_redis_rate_limit_backend_is_not_supported_yet() -> None:
    with pytest.raises(ValidationError):
        make_settings(rate_limit_backend="redis")
