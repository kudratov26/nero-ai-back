"""Application configuration loaded from the environment."""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Literal, Self

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEV_APP_SECRET = "change-me-in-production"
DEFAULT_MAX_BODY_BYTES = 1_048_576

LogLevel = Literal["debug", "info", "warning", "error", "critical"]
RateLimitBackend = Literal["memory", "redis"]


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

    rate_limit_backend: RateLimitBackend = "memory"
    max_request_body_bytes: int = DEFAULT_MAX_BODY_BYTES

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalize_log_level(cls, value: object) -> object:
        return str(value).lower()

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

    @field_validator("rate_limit_backend")
    @classmethod
    def _validate_rate_limit_backend(cls, value: str) -> str:
        # Fail at startup rather than silently running unthrottled in a cluster.
        if value == "redis":
            raise ValueError("RATE_LIMIT_BACKEND=redis is not implemented yet")
        return value

    @model_validator(mode="after")
    def _validate_production(self) -> Self:
        if self.app_env is not AppEnv.PRODUCTION:
            return self
        if self.app_secret == DEV_APP_SECRET:
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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings instance (cached)."""
    return Settings()
