"""Application configuration loaded from the environment."""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEV_APP_SECRET = "change-me-in-production"
DEFAULT_MAX_BODY_BYTES = 1_048_576
DEFAULT_CHAT_MODEL = "deepseek-v4.1-flash"

LogLevel = Literal["debug", "info", "warning", "error", "critical"]
RateLimitBackend = Literal["memory", "postgres", "redis"]
GenerationMode = Literal["mock", "live"]


class AppEnv(StrEnum):
    """Runtime environment. Values match `APP_ENV`."""

    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class Settings(BaseSettings):
    """Validated application settings.

    Values come from environment variables first, then `.env`. Missing required
    values fail fast at startup instead of surfacing as runtime errors.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: AppEnv = AppEnv.DEVELOPMENT
    app_secret: str = DEV_APP_SECRET
    log_level: LogLevel = "info"

    database_url: str
    frontend_origin: str = "http://localhost:3000"

    session_cookie_name: str = "nero_session"
    session_ttl_days: int = 30
    cookie_secure: bool | None = None

    rate_limit_backend: RateLimitBackend = "postgres"
    max_request_body_bytes: int = DEFAULT_MAX_BODY_BYTES

    # Operations. `python -m app.cli ops-check` evaluates these thresholds and
    # routes alerts to structured logs and the optional webhook.
    ops_queue_backlog_threshold: int = 10
    ops_queue_backlog_age_seconds: float = 300.0
    ops_provider_error_threshold: int = 10
    ops_provider_error_window_seconds: float = 900.0
    ops_alert_webhook_url: str | None = None

    # Plan changes are simulated (no payment provider). The endpoint is disabled
    # unless this is true; unset means allowed everywhere except production.
    allow_simulated_plan_changes: bool | None = None

    # Model provider and generation mode. `GENERATION_MODE=live` without an image
    # or video adapter must not advertise those models; the catalog seed enforces it.
    ai_base_url: str | None = None
    ai_api_key: str | None = None
    ai_chat_model: str = DEFAULT_CHAT_MODEL
    ai_request_timeout_seconds: float = 120.0
    generation_mode: GenerationMode = "mock"

    # The worker runs as its own process (`python -m app.worker`). It claims queued
    # Generations, reports progress, and recovers a lock that outlived its worker.
    worker_concurrency: int = 2
    worker_poll_interval_seconds: float = 1.0
    worker_stale_after_seconds: float = 300.0

    # A reservation that outlives this long is considered stranded: its owner
    # never settled it, so the sweeper fails the work and refunds the hold.
    hold_ttl_seconds: float = 3600.0

    # Assets are served from local disk in development, or from an S3-compatible
    # bucket in production. The adapter is chosen once at startup.
    storage_backend: Literal["local", "s3"] = "local"
    storage_dir: Path = Path("./storage")
    s3_bucket: str | None = None
    s3_region: str = "us-east-1"
    s3_endpoint_url: str | None = None
    s3_access_key_id: SecretStr | None = None
    s3_secret_access_key: SecretStr | None = None
    s3_session_token: SecretStr | None = None
    s3_prefix: str = ""

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalize_log_level(cls, value: object) -> object:
        return str(value).lower()

    @field_validator(
        "ai_base_url",
        "ai_api_key",
        "ops_alert_webhook_url",
        "s3_bucket",
        "s3_endpoint_url",
        "s3_access_key_id",
        "s3_secret_access_key",
        "s3_session_token",
        mode="before",
    )
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        """`.env.example` ships empty placeholders; treat them as unset."""
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @field_validator("ai_base_url")
    @classmethod
    def _strip_base_url_slash(cls, value: str | None) -> str | None:
        return None if value is None else value.rstrip("/")

    @field_validator("ai_chat_model")
    @classmethod
    def _require_chat_model(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("AI_CHAT_MODEL must not be blank")
        return trimmed

    @field_validator("session_ttl_days")
    @classmethod
    def _validate_session_ttl(cls, value: int) -> int:
        if value < 1:
            raise ValueError("SESSION_TTL_DAYS must be at least 1")
        return value

    @field_validator("max_request_body_bytes")
    @classmethod
    def _validate_body_limit(cls, value: int) -> int:
        if value < 1:
            raise ValueError("MAX_REQUEST_BODY_BYTES must be at least 1")
        return value

    @field_validator("ai_request_timeout_seconds")
    @classmethod
    def _validate_ai_timeout(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("AI_REQUEST_TIMEOUT_SECONDS must be positive")
        return value

    @field_validator("storage_dir", mode="before")
    @classmethod
    def _require_storage_dir(cls, value: object) -> object:
        """Never let a blank `STORAGE_DIR` resolve to the current directory."""
        if value is None:
            return Path("./storage")
        text = str(value).strip()
        if not text:
            raise ValueError("STORAGE_DIR must not be blank")
        return Path(text)

    @field_validator("s3_region")
    @classmethod
    def _require_s3_region(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("S3_REGION must not be blank")
        return trimmed

    @field_validator("s3_endpoint_url")
    @classmethod
    def _validate_s3_endpoint(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.rstrip("/")
        if not trimmed.startswith(("https://", "http://")):
            raise ValueError("S3_ENDPOINT_URL must be an http(s) URL")
        return trimmed

    @field_validator("s3_prefix")
    @classmethod
    def _normalize_s3_prefix(cls, value: str) -> str:
        trimmed = value.strip().strip("/")
        if "\x00" in trimmed or ".." in trimmed.split("/"):
            raise ValueError("S3_PREFIX must be a safe key prefix")
        return trimmed

    @field_validator("worker_concurrency")
    @classmethod
    def _validate_worker_concurrency(cls, value: int) -> int:
        if value < 1:
            raise ValueError("WORKER_CONCURRENCY must be at least 1")
        return value

    @field_validator("worker_poll_interval_seconds", "worker_stale_after_seconds")
    @classmethod
    def _validate_worker_seconds(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("worker intervals must be positive")
        return value

    @field_validator("hold_ttl_seconds")
    @classmethod
    def _validate_hold_ttl(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("HOLD_TTL_SECONDS must be positive")
        return value

    @field_validator("rate_limit_backend")
    @classmethod
    def _validate_rate_limit_backend(cls, value: str) -> str:
        # Fail at startup rather than silently running unthrottled in a cluster.
        if value == "redis":
            raise ValueError("RATE_LIMIT_BACKEND=redis is not implemented yet")
        return value

    @field_validator("ops_queue_backlog_threshold", "ops_provider_error_threshold")
    @classmethod
    def _validate_ops_threshold(cls, value: int) -> int:
        if value < 1:
            raise ValueError("ops alert thresholds must be at least 1")
        return value

    @field_validator("ops_queue_backlog_age_seconds", "ops_provider_error_window_seconds")
    @classmethod
    def _validate_ops_window(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("ops alert windows must be positive")
        return value

    @field_validator("ops_alert_webhook_url")
    @classmethod
    def _validate_ops_webhook(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.startswith(("https://", "http://")):
            raise ValueError("OPS_ALERT_WEBHOOK_URL must be an http(s) URL")
        return value

    @model_validator(mode="after")
    def _validate_storage(self) -> Self:
        if self.storage_backend == "s3":
            missing = [
                name
                for name, value in (
                    ("S3_BUCKET", self.s3_bucket),
                    ("S3_ACCESS_KEY_ID", self.s3_access_key_id),
                    ("S3_SECRET_ACCESS_KEY", self.s3_secret_access_key),
                )
                if value is None
            ]
            if missing:
                raise ValueError("STORAGE_BACKEND=s3 requires " + ", ".join(missing))
            if (
                self.is_production
                and self.s3_endpoint_url is not None
                and self.s3_endpoint_url.startswith("http://")
            ):
                raise ValueError("S3_ENDPOINT_URL must use https in production")
        return self

    @model_validator(mode="after")
    def _validate_production(self) -> Self:
        if self.app_env is not AppEnv.PRODUCTION:
            return self
        secret = self.app_secret.strip()
        if not secret or secret == DEV_APP_SECRET or secret.startswith("CHANGE_ME"):
            raise ValueError("APP_SECRET must be set to a real value in production")
        if self.cookie_secure is False:
            raise ValueError("COOKIE_SECURE must not be false in production")
        return self

    @property
    def is_production(self) -> bool:
        return self.app_env is AppEnv.PRODUCTION

    @property
    def secure_cookies(self) -> bool:
        """Whether the session cookie must carry the `Secure` flag."""
        if self.cookie_secure is not None:
            return self.cookie_secure
        return self.is_production

    @property
    def simulated_plan_changes_allowed(self) -> bool:
        """Whether `POST /v1/account/plan` is enabled.

        v1 has no billing provider, so plan changes are a simulated refill. They
        are allowed by default everywhere except production, and can be forced on
        or off with `ALLOW_SIMULATED_PLAN_CHANGES`.
        """
        if self.allow_simulated_plan_changes is not None:
            return self.allow_simulated_plan_changes
        return not self.is_production

    @property
    def chat_provider_configured(self) -> bool:
        """Whether the chat gateway is configured well enough to be advertised.

        B2.1 treats a base URL plus an API key as "configured". B3.1 adds a startup
        probe and may tighten this to a verified round trip.
        """
        return bool(self.ai_base_url and self.ai_api_key)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings instance (cached)."""
    return Settings()
