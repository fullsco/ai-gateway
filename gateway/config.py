from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="GATEWAY_",
        extra="ignore",
        case_sensitive=False,
    )

    environment: Literal["development", "test", "staging", "production"] = "development"
    host: str = "127.0.0.1"
    port: int = Field(default=8320, ge=1, le=65535)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["json", "text"] = "json"
    request_id_header: str = "x-request-id"
    trust_incoming_request_id: bool = False
    database_url: str | None = None
    config_refresh_seconds: float = Field(default=5, gt=0)
    request_timeout_seconds: float = Field(default=600, gt=0)
    first_event_timeout_seconds: float = Field(default=60, gt=0)
    concurrency_acquire_timeout_seconds: float = Field(default=1, gt=0)
    failover_enabled: bool = True
    health_probe_enabled: bool = True
    health_probe_interval_seconds: float = Field(default=60, gt=0)
    health_probe_daily_limit: int = Field(default=10, ge=1, le=100)
    health_probe_min_interval_seconds: int = Field(default=7200, ge=60)
    health_probe_lease_seconds: int = Field(default=30, ge=15)
    health_probe_failure_backoff_seconds: int = Field(default=7200, ge=60)
    health_probe_max_backoff_seconds: int = Field(default=86400, ge=60)
    health_probe_manual_daily_limit: int = Field(default=20, ge=1, le=100)
    health_probe_manual_min_interval_seconds: int = Field(default=60, ge=1)
    credential_encryption_key: str | None = None
    key_pepper: str | None = None
    supabase_url: str | None = None
    supabase_jwt_audience: str = "authenticated"
    admin_role: str = "admin"

    @field_validator("request_id_header")
    @classmethod
    def validate_request_id_header(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized or any(char.isspace() for char in normalized):
            raise ValueError("request_id_header must be a valid single HTTP header name")
        return normalized


@lru_cache
def get_settings() -> Settings:
    return Settings()
