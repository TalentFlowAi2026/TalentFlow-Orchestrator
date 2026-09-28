"""Validated server-only configuration for the independent service."""

from __future__ import annotations

import base64
import binascii
from datetime import time
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    environment: Literal["development", "test", "staging", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    api_host: str = "0.0.0.0"
    api_port: int = Field(default=8080, ge=1, le=65535)

    supabase_url: str = ""
    supabase_db_url: SecretStr = SecretStr("")
    supabase_auth_mode: Literal["jwks", "user_endpoint"] = "jwks"
    supabase_publishable_key: SecretStr = SecretStr("")
    jwt_audience: str = "authenticated"
    jwks_cache_seconds: int = Field(default=300, ge=30, le=600)

    database_pool_min: int = Field(default=1, ge=1, le=10)
    database_pool_max: int = Field(default=10, ge=1, le=50)
    api_database_pool_max: int = Field(default=4, ge=1, le=20)
    background_database_pool_max: int = Field(default=6, ge=1, le=30)

    provider_timeout_seconds: float = Field(default=30, gt=0, le=120)
    worker_poll_seconds: float = Field(default=1, ge=0.1, le=30)
    worker_concurrency: int = Field(default=4, ge=1, le=50)
    job_lease_seconds: int = Field(default=120, ge=30, le=900)
    job_timeout_seconds: int = Field(default=90, ge=5, le=600)
    job_max_attempts: int = Field(default=5, ge=1, le=10)
    shutdown_grace_seconds: int = Field(default=120, ge=10, le=900)

    proposal_expiry_minutes: int = Field(default=30, ge=5, le=1440)
    max_scheduling_horizon_days: int = Field(default=31, ge=1, le=92)
    backend_max_parallel_ai_interviews: int = Field(default=1, ge=1, le=100)
    default_interview_day_start: time = time(7)
    default_interview_day_end: time = time(17)
    max_request_bytes: int = Field(default=32768, ge=4096, le=131072)
    api_requests_per_minute: int = Field(default=60, ge=1, le=600)
    public_interview_base_url: str = "http://localhost:3000/interview"
    data_encryption_key: SecretStr = SecretStr("")
    data_encryption_key_version: str = Field(default="v1", min_length=1, max_length=40)

    google_oauth_client_id: str = ""
    google_oauth_client_secret: SecretStr = SecretStr("")
    google_oauth_redirect_uri: str = ""
    google_calendar_enabled: bool = False
    gmail_enabled: bool = False

    google_api_key: SecretStr = SecretStr("")
    gemini_scheduling_model: str = "gemini-3.1-pro-preview"
    gemini_intent_timeout_seconds: float = Field(default=30, gt=0, le=120)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.database_pool_min > self.database_pool_max:
            raise ValueError("database pool minimum exceeds maximum")
        if max(self.api_database_pool_max, self.background_database_pool_max) > self.database_pool_max:
            raise ValueError("role database pool maximum exceeds DATABASE_POOL_MAX")
        if self.database_pool_min > min(
            self.api_database_pool_max, self.background_database_pool_max
        ):
            raise ValueError("database pool minimum exceeds a role pool maximum")
        if self.job_lease_seconds <= self.job_timeout_seconds + 10:
            raise ValueError("job lease must exceed timeout by more than 10 seconds")
        if self.shutdown_grace_seconds <= self.job_timeout_seconds:
            raise ValueError("shutdown grace must exceed job timeout")
        if self.default_interview_day_end <= self.default_interview_day_start:
            raise ValueError("default interview day end must be later than start")

        base = urlsplit(self.public_interview_base_url)
        if base.scheme not in {"http", "https"} or not base.netloc:
            raise ValueError("PUBLIC_INTERVIEW_BASE_URL must be an absolute HTTP(S) URL")

        integrations_enabled = self.google_calendar_enabled or self.gmail_enabled
        encryption_key = self.data_encryption_key.get_secret_value()
        if encryption_key:
            try:
                key = base64.b64decode(
                    encryption_key + "=" * (-len(encryption_key) % 4),
                    altchars=b"-_",
                    validate=True,
                )
            except (binascii.Error, ValueError) as exc:
                raise ValueError("DATA_ENCRYPTION_KEY must be base64url") from exc
            if len(key) != 32:
                raise ValueError("DATA_ENCRYPTION_KEY must decode to 32 bytes")
        if integrations_enabled:
            required = {
                "GOOGLE_OAUTH_CLIENT_ID": self.google_oauth_client_id,
                "GOOGLE_OAUTH_CLIENT_SECRET": self.google_oauth_client_secret.get_secret_value(),
                "GOOGLE_OAUTH_REDIRECT_URI": self.google_oauth_redirect_uri,
                "DATA_ENCRYPTION_KEY": self.data_encryption_key.get_secret_value(),
            }
            missing = [name for name, value in required.items() if not value]
            if missing:
                raise ValueError("Google integration configuration missing: " + ", ".join(missing))
            redirect = urlsplit(self.google_oauth_redirect_uri)
            if redirect.scheme not in {"http", "https"} or not redirect.netloc:
                raise ValueError("GOOGLE_OAUTH_REDIRECT_URI must be an absolute HTTP(S) URL")

        if self.environment in {"staging", "production"}:
            if urlsplit(self.supabase_url).scheme != "https":
                raise ValueError("SUPABASE_URL requires HTTPS")
            db_url = self.supabase_db_url.get_secret_value()
            if "sslmode=" not in db_url or "sslmode=disable" in db_url:
                raise ValueError("database URL requires TLS")
            if base.scheme != "https":
                raise ValueError("PUBLIC_INTERVIEW_BASE_URL requires HTTPS")
            if not self.data_encryption_key.get_secret_value():
                raise ValueError("DATA_ENCRYPTION_KEY is required")
            if integrations_enabled and urlsplit(self.google_oauth_redirect_uri).scheme != "https":
                raise ValueError("GOOGLE_OAUTH_REDIRECT_URI requires HTTPS")
        if self.environment == "production" and (
            base.hostname != "talant-flow.app" or base.path.rstrip("/") != "/interview"
        ):
            raise ValueError(
                "PUBLIC_INTERVIEW_BASE_URL must be https://talant-flow.app/interview"
            )
        return self

    def missing(self, role: Literal["api", "background"]) -> list[str]:
        names = ["supabase_db_url", "data_encryption_key"]
        if role == "api":
            names.append("supabase_url")
            if self.supabase_auth_mode == "user_endpoint":
                names.append("supabase_publishable_key")
        result: list[str] = []
        for name in names:
            value = getattr(self, name)
            if not (value.get_secret_value() if isinstance(value, SecretStr) else value):
                result.append(name.upper())
        return result
