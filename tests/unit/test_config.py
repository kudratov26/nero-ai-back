"""Tests for environment-driven configuration."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import DEFAULT_CHAT_MODEL, DEV_APP_SECRET, AppEnv, Settings
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


@pytest.mark.parametrize(
    "secret",
    [DEV_APP_SECRET, "", "   ", "CHANGE_ME_USE_OPENSSL_RAND_HEX_32"],
)
def test_production_requires_real_secret(secret: str) -> None:
    with pytest.raises(ValidationError):
        make_settings(app_env="production", app_secret=secret)


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


def test_postgres_rate_limit_backend_is_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RATE_LIMIT_BACKEND", raising=False)
    settings = Settings(_env_file=None, database_url="postgresql+asyncpg://localhost/nero")
    assert settings.rate_limit_backend == "postgres"


def test_memory_rate_limit_backend_is_available() -> None:
    assert make_settings(rate_limit_backend="memory").rate_limit_backend == "memory"


def test_redis_rate_limit_backend_is_not_supported_yet() -> None:
    with pytest.raises(ValidationError):
        make_settings(rate_limit_backend="redis")


def test_generation_mode_defaults_to_mock() -> None:
    assert make_settings().generation_mode == "mock"


def test_storage_defaults_to_local_disk() -> None:
    settings = make_settings()

    assert settings.storage_backend == "local"
    assert settings.storage_dir == Path("storage")


def test_storage_dir_must_not_be_blank() -> None:
    with pytest.raises(ValidationError):
        make_settings(storage_dir="   ")


def test_remote_storage_backend_is_not_supported_yet() -> None:
    with pytest.raises(ValidationError):
        make_settings(storage_backend="s3")


def test_invalid_generation_mode_rejected() -> None:
    with pytest.raises(ValidationError):
        make_settings(generation_mode="shadow")


def test_chat_model_defaults_to_deepseek_flash() -> None:
    assert make_settings().ai_chat_model == DEFAULT_CHAT_MODEL


def test_chat_model_must_not_be_blank() -> None:
    with pytest.raises(ValidationError):
        make_settings(ai_chat_model="  ")


def test_chat_model_is_trimmed() -> None:
    assert make_settings(ai_chat_model="  custom-chat  ").ai_chat_model == "custom-chat"


@pytest.mark.parametrize(
    ("base_url", "api_key"),
    [
        (None, None),
        ("https://gateway.example.test", None),
        (None, "a-key"),
        ("", "a-key"),
        ("https://gateway.example.test", ""),
        ("   ", "a-key"),
    ],
)
def test_chat_provider_is_unconfigured_until_both_values_are_set(
    base_url: str | None, api_key: str | None
) -> None:
    settings = make_settings(ai_base_url=base_url, ai_api_key=api_key)

    assert settings.chat_provider_configured is False


def test_chat_provider_is_configured_with_a_base_url_and_key() -> None:
    settings = make_settings(
        ai_base_url="https://gateway.example.test/v1",
        ai_api_key="a-key",
    )

    assert settings.chat_provider_configured is True


def test_base_url_trailing_slash_is_removed() -> None:
    settings = make_settings(ai_base_url="https://gateway.example.test/v1/")

    assert settings.ai_base_url == "https://gateway.example.test/v1"


def test_empty_env_placeholders_become_none() -> None:
    settings = make_settings(ai_base_url="", ai_api_key="")

    assert settings.ai_base_url is None
    assert settings.ai_api_key is None


def test_simulated_plan_changes_default_to_on_outside_production() -> None:
    assert make_settings().simulated_plan_changes_allowed is True


def test_simulated_plan_changes_default_to_off_in_production() -> None:
    settings = make_settings(
        app_env="production",
        app_secret="a-real-production-secret",
    )

    assert settings.is_production is True
    assert settings.simulated_plan_changes_allowed is False


@pytest.mark.parametrize("allowed", [True, False])
def test_simulated_plan_changes_can_be_forced(allowed: bool) -> None:
    settings = make_settings(
        app_env="production",
        app_secret="a-real-production-secret",
        allow_simulated_plan_changes=allowed,
    )

    assert settings.simulated_plan_changes_allowed is allowed


def test_ai_timeout_defaults_to_120_seconds() -> None:
    assert make_settings().ai_request_timeout_seconds == 120.0


def test_ai_timeout_can_be_overridden() -> None:
    assert make_settings(ai_request_timeout_seconds=5).ai_request_timeout_seconds == 5.0


@pytest.mark.parametrize("value", [0, -1, -0.5])
def test_ai_timeout_must_be_positive(value: float) -> None:
    with pytest.raises(ValidationError):
        make_settings(ai_request_timeout_seconds=value)


def test_worker_concurrency_defaults_to_two() -> None:
    assert make_settings().worker_concurrency == 2


def test_worker_concurrency_can_be_overridden() -> None:
    assert make_settings(worker_concurrency=5).worker_concurrency == 5


@pytest.mark.parametrize("value", [0, -1])
def test_worker_concurrency_must_be_at_least_one(value: int) -> None:
    with pytest.raises(ValidationError):
        make_settings(worker_concurrency=value)


def test_worker_intervals_have_development_defaults() -> None:
    settings = make_settings()

    assert settings.worker_poll_interval_seconds == 1.0
    assert settings.worker_stale_after_seconds == 300.0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("worker_poll_interval_seconds", 0),
        ("worker_poll_interval_seconds", -1),
        ("worker_stale_after_seconds", 0),
        ("worker_stale_after_seconds", -0.5),
    ],
)
def test_worker_intervals_must_be_positive(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        make_settings(**{field: value})


def test_hold_ttl_defaults_to_one_hour() -> None:
    assert make_settings().hold_ttl_seconds == 3_600.0


@pytest.mark.parametrize("value", [0, -1, -0.5])
def test_hold_ttl_must_be_positive(value: float) -> None:
    with pytest.raises(ValidationError):
        make_settings(hold_ttl_seconds=value)


def test_ops_alert_defaults() -> None:
    settings = make_settings()

    assert settings.ops_queue_backlog_threshold == 10
    assert settings.ops_queue_backlog_age_seconds == 300.0
    assert settings.ops_provider_error_threshold == 10
    assert settings.ops_provider_error_window_seconds == 900.0
    assert settings.ops_alert_webhook_url is None


@pytest.mark.parametrize(
    "field",
    ["ops_queue_backlog_threshold", "ops_provider_error_threshold"],
)
@pytest.mark.parametrize("value", [0, -1])
def test_ops_thresholds_must_be_at_least_one(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        make_settings(**{field: value})


@pytest.mark.parametrize(
    "field",
    ["ops_queue_backlog_age_seconds", "ops_provider_error_window_seconds"],
)
@pytest.mark.parametrize("value", [0, -0.5])
def test_ops_windows_must_be_positive(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        make_settings(**{field: value})


def test_ops_webhook_accepts_http_urls() -> None:
    settings = make_settings(ops_alert_webhook_url="https://hooks.example.test/alerts")

    assert settings.ops_alert_webhook_url == "https://hooks.example.test/alerts"


def test_ops_webhook_rejects_a_non_http_scheme() -> None:
    with pytest.raises(ValidationError):
        make_settings(ops_alert_webhook_url="ftp://hooks.example.test/alerts")


def test_a_blank_ops_webhook_is_unset() -> None:
    assert make_settings(ops_alert_webhook_url="   ").ops_alert_webhook_url is None


S3_SETTINGS = {
    "storage_backend": "s3",
    "s3_bucket": "nero-assets",
    "s3_access_key_id": "AKIAIOSFODNN7EXAMPLE",
    "s3_secret_access_key": "not-a-real-secret",
}


def test_storage_defaults_to_local() -> None:
    settings = make_settings()

    assert settings.storage_backend == "local"
    assert settings.s3_region == "us-east-1"
    assert settings.s3_prefix == ""
    assert settings.s3_bucket is None


def test_s3_settings_are_accepted_and_redacted() -> None:
    settings = make_settings(**S3_SETTINGS)

    assert settings.storage_backend == "s3"
    assert settings.s3_access_key_id is not None
    assert settings.s3_access_key_id.get_secret_value() == "AKIAIOSFODNN7EXAMPLE"
    assert "not-a-real-secret" not in repr(settings)


@pytest.mark.parametrize("field", ["s3_bucket", "s3_access_key_id", "s3_secret_access_key"])
def test_s3_requires_each_credential(field: str) -> None:
    values = dict(S3_SETTINGS)
    values[field] = None

    with pytest.raises(ValidationError):
        make_settings(**values)


def test_blank_s3_credentials_are_unset() -> None:
    values = dict(S3_SETTINGS, s3_access_key_id="   ")

    with pytest.raises(ValidationError):
        make_settings(**values)


def test_an_s3_endpoint_loses_its_trailing_slash() -> None:
    settings = make_settings(**S3_SETTINGS, s3_endpoint_url="https://s3.example.test/")

    assert settings.s3_endpoint_url == "https://s3.example.test"


def test_an_s3_endpoint_must_be_http_or_https() -> None:
    with pytest.raises(ValidationError):
        make_settings(**S3_SETTINGS, s3_endpoint_url="ftp://s3.example.test")


def test_production_rejects_a_plaintext_s3_endpoint() -> None:
    with pytest.raises(ValidationError):
        make_settings(
            **S3_SETTINGS,
            s3_endpoint_url="http://minio.internal:9000",
            app_env="production",
            app_secret="a-real-production-secret",
        )


def test_production_accepts_an_https_s3_endpoint() -> None:
    settings = make_settings(
        **S3_SETTINGS,
        s3_endpoint_url="https://s3.example.test",
        app_env="production",
        app_secret="a-real-production-secret",
    )

    assert settings.s3_endpoint_url == "https://s3.example.test"


def test_the_s3_prefix_is_normalized() -> None:
    assert make_settings(s3_prefix="/nero/assets/").s3_prefix == "nero/assets"


@pytest.mark.parametrize("value", ["../escape", "a/../b", "a\x00b"])
def test_the_s3_prefix_cannot_escape_the_bucket(value: str) -> None:
    with pytest.raises(ValidationError):
        make_settings(**S3_SETTINGS, s3_prefix=value)


def test_the_s3_region_must_not_be_blank() -> None:
    with pytest.raises(ValidationError):
        make_settings(**S3_SETTINGS, s3_region="   ")
