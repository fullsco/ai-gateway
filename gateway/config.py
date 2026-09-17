import base64
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
    config_refresh_seconds: float = Field(default=60, gt=0)
    # How often live operational state (health, cooldown, quota) is re-read from the
    # database. In-process attempt outcomes apply immediately regardless of this.
    live_state_refresh_seconds: float = Field(default=30, gt=0)
    alert_monitor_enabled: bool = True
    alert_monitor_interval_seconds: float = Field(default=60, gt=0)
    credential_usage_poll_enabled: bool = False
    credential_usage_poll_interval_seconds: float = Field(default=900, gt=0)
    quota_soft_threshold: float = Field(default=0.15, ge=0, le=1)
    quota_hard_threshold: float = Field(default=0.02, ge=0, le=1)
    request_timeout_seconds: float = Field(default=600, gt=0)
    # Per-process admission. Global provider/tenant limits need distributed coordination.
    max_in_flight_requests: int = Field(default=32, ge=1)
    max_request_body_bytes: int = Field(default=8 * 1024 * 1024, ge=1)
    # Input-byte accounting held through response completion; parsed JSON uses more RAM.
    max_in_flight_body_bytes: int = Field(default=64 * 1024 * 1024, ge=1)
    request_body_timeout_seconds: float = Field(default=30, gt=0, allow_inf_nan=False)
    overload_retry_after_seconds: int = Field(default=1, ge=1)
    trace_sample_rate: float = Field(default=.01, ge=0, le=1, allow_inf_nan=False)
    max_in_flight_per_client: int = Field(default=8, ge=1)
    max_in_flight_streams: int = Field(default=24, ge=1)
    max_output_tokens: int = Field(default=32768, ge=1)
    max_tools: int = Field(default=128, ge=1)
    max_tool_payload_bytes: int = Field(default=1024 * 1024, ge=1)
    max_multimodal_bytes: int = Field(default=4 * 1024 * 1024, ge=1)
    http_max_connections: int = Field(default=64, ge=1)
    http_max_keepalive_connections: int = Field(default=32, ge=0)
    http_keepalive_expiry_seconds: float = Field(default=30, gt=0, allow_inf_nan=False)
    http_connect_timeout_seconds: float = Field(default=10, gt=0, allow_inf_nan=False)
    http_pool_timeout_seconds: float = Field(default=1, gt=0, allow_inf_nan=False)
    http_write_timeout_seconds: float = Field(default=30, gt=0, allow_inf_nan=False)
    durable_accounting_enabled: bool = False
    # Opt-in telemetry pruning alongside accounting; artifact expiry is always on.
    maintenance_enabled: bool = False
    # Telemetry cadence; artifacts retain the accounting worker's five-second cadence.
    maintenance_interval_seconds: float = Field(
        default=3600, ge=60, le=86400, allow_inf_nan=False,
    )
    # Separate deadline for artifact GC and telemetry pruning.
    maintenance_timeout_seconds: float = Field(
        default=5, ge=0.1, le=60, allow_inf_nan=False,
    )
    telemetry_retention_days: int = Field(default=30, ge=7, le=365)
    workflows_enabled: bool = False
    workflow_global_capacity: int = Field(default=4, ge=1, le=32)
    workflow_per_client_capacity: int = Field(default=1, ge=1, le=8)
    workflow_max_pending_per_client: int = Field(default=32, ge=1, le=1000)
    artifact_encryption_key: str | None = Field(default=None, repr=False)
    distributed_admission_enabled: bool = False
    distributed_max_requests: int = Field(default=64, ge=1)
    distributed_max_per_client: int = Field(default=16, ge=1)
    distributed_max_streams: int = Field(default=48, ge=1)
    distributed_provider_capacity: int = Field(default=32, ge=1)
    distributed_credential_capacity: int = Field(default=8, ge=1)
    service_role: Literal["combined", "api", "workflow", "maintenance"] = "combined"
    shutdown_timeout_seconds: float = Field(default=30, gt=0, allow_inf_nan=False)
    first_event_timeout_seconds: float = Field(default=60, gt=0)
    # How long a committed stream may go silent before a no-op event is sent to keep
    # the client connection warm. A CDN in front of the gateway measures its read
    # timeout between bytes delivered to the client, not between useful ones, so a
    # model that thinks for longer than that window has its connection killed at the
    # edge while the gateway and the provider are both still healthy and working. The
    # client sees a 524, or a stream that stops and cannot be told from a finished one.
    # Must stay well under the edge timeout; 0 disables and restores raw relaying.
    stream_keepalive_seconds: float = Field(default=15, ge=0)
    # Some provider edges challenge on the caller's source IP, so a datacenter
    # egress range can be refused no matter what the request looks like. Routing
    # upstream traffic through a proxy with a clean reputation fixes that without
    # changing any provider or protocol behaviour.
    upstream_proxy_url: str | None = None
    upstream_proxy_verify_tls: bool = True
    concurrency_acquire_timeout_seconds: float = Field(default=1, gt=0)
    failover_enabled: bool = True
    retry_backoff_seconds: float = Field(default=.05, ge=0, le=5, allow_inf_nan=False)
    retry_backoff_max_seconds: float = Field(default=1, ge=0, le=30, allow_inf_nan=False)
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

    @field_validator("artifact_encryption_key")
    @classmethod
    def validate_artifact_key(cls, value):
        if value is not None and len(base64.b64decode(value, validate=True)) != 32:
            raise ValueError("Artifact key must decode to 32 bytes")
        return value

    @field_validator("upstream_proxy_url")
    @classmethod
    def validate_upstream_proxy_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            return None
        allowed = ("http://", "https://", "socks5://", "socks5h://")
        if not normalized.startswith(allowed):
            raise ValueError(
                "upstream_proxy_url must start with one of: " + ", ".join(allowed)
            )
        return normalized


@lru_cache
def get_settings() -> Settings:
    return Settings()
