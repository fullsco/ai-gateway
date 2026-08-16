import asyncio
import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field


def _strip_runtime_state(payload: dict[str, Any]) -> dict[str, Any]:
    projected = json.loads(json.dumps(payload, default=str))
    for row in projected.get("providers", []):
        row.pop("health", None)
    for row in projected.get("credentials", []):
        for field in ("health", "quota_used", "cooldown_until"):
            row.pop(field, None)
    return projected


def configuration_projection(payload: dict[str, Any]) -> dict[str, Any]:
    projected = _strip_runtime_state(payload)
    for rows in projected.values():
        if isinstance(rows, list) and all(isinstance(row, dict) for row in rows):
            rows.sort(
                key=lambda row: str(
                    row.get("id", row.get("key_prefix", row.get("name", "")))
                )
            )
    return projected


def configuration_checksum(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        configuration_projection(payload),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def legacy_configuration_checksum(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        _strip_runtime_state(payload),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def legacy_checksum(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ConfigSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    version: int = Field(ge=1)
    schema_version: int = Field(default=1, ge=1)
    checksum: str = Field(min_length=64, max_length=64)
    published_at: datetime
    payload: dict[str, Any]

    @classmethod
    def create(
        cls,
        *,
        version: int,
        payload: dict[str, Any],
        published_at: datetime | None = None,
    ) -> "ConfigSnapshot":
        return cls(
            version=version,
            checksum=configuration_checksum(payload),
            published_at=published_at or datetime.now(UTC),
            payload=payload,
        )

    def verify_checksum(self) -> bool:
        return self.checksum in {
            configuration_checksum(self.payload),
            legacy_configuration_checksum(self.payload),
            legacy_checksum(self.payload),
        }


class SnapshotRepository(Protocol):
    async def load_published(self) -> ConfigSnapshot | None:
        """Return the currently published, validated configuration snapshot."""


class ConfigurationUnavailable(RuntimeError):
    pass


class CachedConfiguration:
    def __init__(self, repository: SnapshotRepository) -> None:
        self._repository = repository
        self._snapshot: ConfigSnapshot | None = None
        self._lock = asyncio.Lock()
        self._last_refresh_error: str | None = None

    @property
    def snapshot(self) -> ConfigSnapshot:
        if self._snapshot is None:
            raise ConfigurationUnavailable("No valid configuration snapshot is loaded")
        return self._snapshot

    @property
    def last_refresh_error(self) -> str | None:
        return self._last_refresh_error

    async def refresh(self) -> ConfigSnapshot:
        async with self._lock:
            try:
                candidate = await self._repository.load_published()
                if candidate is None:
                    raise ConfigurationUnavailable("No published configuration exists")
                if not candidate.verify_checksum():
                    raise ConfigurationUnavailable("Published configuration checksum is invalid")
                if self._snapshot is None or candidate.version >= self._snapshot.version:
                    self._snapshot = candidate
                self._last_refresh_error = None
            except Exception as exc:
                self._last_refresh_error = type(exc).__name__
                if self._snapshot is None:
                    raise ConfigurationUnavailable(
                        "Configuration refresh failed and no cached snapshot is available"
                    ) from exc
            return self.snapshot
