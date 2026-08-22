import asyncio
import hashlib
import json
import logging
from collections import Counter
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from gateway.logging import log_event

logger = logging.getLogger("gateway.configuration")


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


def _index(rows: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(rows, list):
        return {}
    return {
        str(row["id"]): row
        for row in rows
        if isinstance(row, dict) and row.get("id") is not None
    }


def _plural(count: int) -> str:
    return "" if count == 1 else "s"


def _value(value: Any) -> str:
    if value is None or value == "":
        return "none"
    return str(value)


def _transition(label: str, before: Any, after: Any) -> str:
    return f"{label} {_value(before)} to {_value(after)}"


def _enabled_transition(_label: str, _before: Any, after: Any) -> str:
    return "enabled" if after else "disabled"


def _toggle_transition(label: str, _before: Any, after: Any) -> str:
    return f"{label} {'on' if after else 'off'}"


def _list_transition(label: str, _before: Any, after: Any) -> str:
    values = ", ".join(str(item) for item in (after or []))
    return f"{label} now {values or 'none'}"


def _pricing_transition(before: Any, after: Any) -> str:
    before = before or {}
    after = after or {}
    if before == after:
        return ""
    if not before:
        return "pricing added"
    if not after:
        return "pricing removed"
    return "pricing updated"


def _change(change: str, resource: str, summary: str) -> dict[str, str]:
    return {"change": change, "resource": resource, "summary": summary}


def _field_changes(
    before: dict[str, Any],
    after: dict[str, Any],
    specs: list[tuple[str, str, Any]],
) -> str:
    fragments = [
        formatter(label, before.get(field), after.get(field))
        for field, label, formatter in specs
        if before.get(field) != after.get(field)
    ]
    return ", ".join(fragment for fragment in fragments if fragment)


def _provider_names(*projections: dict[str, Any]) -> dict[str, str]:
    names: dict[str, str] = {}
    for projection in projections:
        for row in projection.get("providers", []) or []:
            if isinstance(row, dict) and row.get("id") is not None:
                names[str(row["id"])] = str(row.get("name", row["id"]))
    return names


def _provider_changes(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, str]]:
    prior = _index(before.get("providers"))
    current = _index(after.get("providers"))
    changes: list[dict[str, str]] = []
    for key in sorted(current.keys() - prior.keys()):
        row = current[key]
        state = "enabled" if row.get("enabled", True) else "disabled"
        name = row.get("name", key)
        changes.append(_change("added", "Provider", f"Added provider {name} ({state})"))
    for key in sorted(prior.keys() - current.keys()):
        name = prior[key].get("name", key)
        changes.append(_change("removed", "Provider", f"Removed provider {name}"))
    specs = [
        ("enabled", "", _enabled_transition),
        ("base_url", "base URL", _transition),
        ("priority", "priority", _transition),
        ("timeout_seconds", "timeout seconds", _transition),
        ("capabilities", "capabilities", _list_transition),
        ("auth_scheme", "authentication", _transition),
    ]
    for key in sorted(prior.keys() & current.keys()):
        details = _field_changes(prior[key], current[key], specs)
        if details:
            name = current[key].get("name", key)
            changes.append(_change("updated", "Provider", f"Changed provider {name}: {details}"))
    return changes


def _credential_changes(
    before: dict[str, Any], after: dict[str, Any], names: dict[str, str]
) -> list[dict[str, str]]:
    prior = _index(before.get("credentials"))
    current = _index(after.get("credentials"))

    def provider_of(row: dict[str, Any]) -> str:
        return names.get(str(row.get("provider_id")), "a provider")

    def settings(row: dict[str, Any]) -> tuple[Any, ...]:
        return (
            row.get("priority"),
            row.get("quota_limit"),
            row.get("requests_per_minute"),
            row.get("tokens_per_minute"),
        )

    added: Counter[str] = Counter()
    removed: Counter[str] = Counter()
    updated: Counter[str] = Counter()
    toggled: list[tuple[str, str]] = []
    for key in current.keys() - prior.keys():
        added[provider_of(current[key])] += 1
    for key in prior.keys() - current.keys():
        removed[provider_of(prior[key])] += 1
    for key in prior.keys() & current.keys():
        before_row, after_row = prior[key], current[key]
        if before_row.get("enabled", True) != after_row.get("enabled", True):
            state = "Enabled" if after_row.get("enabled", True) else "Disabled"
            toggled.append((provider_of(after_row), state))
        elif settings(before_row) != settings(after_row):
            updated[provider_of(after_row)] += 1
    changes: list[dict[str, str]] = []
    for provider, count in sorted(added.items()):
        changes.append(
            _change(
                "added",
                "Credentials",
                f"Added {count} credential{_plural(count)} to {provider}",
            )
        )
    for provider, count in sorted(removed.items()):
        changes.append(
            _change(
                "removed",
                "Credentials",
                f"Removed {count} credential{_plural(count)} from {provider}",
            )
        )
    for provider, state in sorted(toggled):
        changes.append(_change("updated", "Credentials", f"{state} a credential on {provider}"))
    for provider, count in sorted(updated.items()):
        changes.append(
            _change(
                "updated",
                "Credentials",
                f"Updated limits on {count} credential{_plural(count)} for {provider}",
            )
        )
    return changes


def _model_changes(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, str]]:
    prior = _index(before.get("models"))
    current = _index(after.get("models"))
    changes: list[dict[str, str]] = []
    for key in sorted(current.keys() - prior.keys()):
        changes.append(_change("added", "Model", f"Added model {key}"))
    for key in sorted(prior.keys() - current.keys()):
        changes.append(_change("removed", "Model", f"Removed model {key}"))
    specs = [
        ("enabled", "", _enabled_transition),
        ("capabilities", "capabilities", _list_transition),
        ("aliases", "aliases", _list_transition),
    ]
    for key in sorted(prior.keys() & current.keys()):
        details = _field_changes(prior[key], current[key], specs)
        if details:
            changes.append(_change("updated", "Model", f"Changed model {key}: {details}"))
    return changes


def _route_changes(
    before: dict[str, Any], after: dict[str, Any], names: dict[str, str]
) -> list[dict[str, str]]:
    prior = _index(before.get("provider_models"))
    current = _index(after.get("provider_models"))

    def label(row: dict[str, Any]) -> str:
        model = row.get("canonical_model_id", "model")
        provider = names.get(str(row.get("provider_id")), "a provider")
        return f"{model} via {provider}"

    changes: list[dict[str, str]] = []
    for key in sorted(current.keys() - prior.keys()):
        changes.append(_change("added", "Model routing", f"Added route: {label(current[key])}"))
    for key in sorted(prior.keys() - current.keys()):
        changes.append(_change("removed", "Model routing", f"Removed route: {label(prior[key])}"))
    specs = [
        ("enabled", "", _enabled_transition),
        ("priority", "priority", _transition),
        ("weight", "traffic share", _transition),
        ("allow_model_fallback", "model fallback", _toggle_transition),
        ("pool_strategy", "strategy", _transition),
        ("protocol", "protocol", _transition),
        # A route timeout changes how long a caller waits and how long a
        # concurrency slot is held, so a publish must say when it moves. Without
        # this the section reported as changed with nothing listed under it, which
        # is worse than silence: the operator is told something changed and not what.
        ("timeout_seconds", "timeout in seconds", _transition),
        ("upstream_model_id", "upstream model", _transition),
        ("max_concurrency", "max concurrency", _transition),
    ]
    for key in sorted(prior.keys() & current.keys()):
        details = _field_changes(prior[key], current[key], specs)
        pricing = _pricing_transition(prior[key].get("pricing"), current[key].get("pricing"))
        combined = ", ".join(fragment for fragment in (details, pricing) if fragment)
        if combined:
            changes.append(
                _change(
                    "updated",
                    "Model routing",
                    f"Changed route {label(current[key])}: {combined}",
                )
            )
    return changes


def _client_changes(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, str]]:
    prior = _index(before.get("clients"))
    current = _index(after.get("clients"))
    changes: list[dict[str, str]] = []
    for key in sorted(current.keys() - prior.keys()):
        name = current[key].get("name", key)
        changes.append(_change("added", "Gateway client", f"Added client {name}"))
    for key in sorted(prior.keys() - current.keys()):
        name = prior[key].get("name", key)
        changes.append(_change("removed", "Gateway client", f"Removed client {name}"))
    specs = [
        ("enabled", "", _enabled_transition),
        ("allowed_protocols", "allowed protocols", _list_transition),
        ("allowed_models", "allowed models", _list_transition),
        ("requests_per_minute", "requests per minute", _transition),
        ("tokens_per_minute", "tokens per minute", _transition),
        ("spending_limit", "spending limit", _transition),
    ]
    for key in sorted(prior.keys() & current.keys()):
        details = _field_changes(prior[key], current[key], specs)
        if details:
            name = current[key].get("name", key)
            changes.append(
                _change("updated", "Gateway client", f"Changed client {name}: {details}")
            )
    return changes


def _key_changes(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, str]]:
    prior = _index(before.get("gateway_keys"))
    current = _index(after.get("gateway_keys"))
    changes: list[dict[str, str]] = []
    for key in sorted(current.keys() - prior.keys()):
        prefix = current[key].get("key_prefix", "")
        changes.append(_change("added", "Gateway key", f"Issued key {prefix}".strip()))
    for key in sorted(prior.keys() - current.keys()):
        prefix = prior[key].get("key_prefix", "")
        changes.append(_change("removed", "Gateway key", f"Revoked key {prefix}".strip()))
    specs = [
        ("enabled", "", _enabled_transition),
        ("expires_at", "expiry", _transition),
    ]
    for key in sorted(prior.keys() & current.keys()):
        details = _field_changes(prior[key], current[key], specs)
        if details:
            prefix = current[key].get("key_prefix", "")
            changes.append(
                _change("updated", "Gateway key", f"Changed key {prefix}: {details}".strip())
            )
    return changes


def summarize_configuration_changes(
    published: dict[str, Any] | None,
    working: dict[str, Any] | None,
) -> list[dict[str, str]]:
    """Describe the difference between two configuration payloads for operators.

    Returns human-readable change entries. Secrets and digests are never surfaced;
    credentials and keys are referenced by provider name or key prefix only.
    """
    before = configuration_projection(published or {})
    after = configuration_projection(working or {})
    names = _provider_names(before, after)
    return [
        *_provider_changes(before, after),
        *_credential_changes(before, after, names),
        *_model_changes(before, after),
        *_route_changes(before, after, names),
        *_client_changes(before, after),
        *_key_changes(before, after),
    ]


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

    # A snapshot load that never returns is worse than one that fails. The lock below
    # serialises refreshes, so a single hung database connection holds it forever and
    # every later refresh blocks behind it. That happened in production: a DNS outage
    # left a connection hanging, configuration froze at version 245, and the gateway
    # went on serving it for hours while the dashboard correctly showed 248 as
    # published. Nothing logged, because a hung await is not an error.
    LOAD_TIMEOUT_SECONDS = 20.0

    async def refresh(self) -> ConfigSnapshot:
        async with self._lock:
            try:
                candidate = await asyncio.wait_for(
                    self._repository.load_published(),
                    timeout=self.LOAD_TIMEOUT_SECONDS,
                )
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
                # Serving the cached snapshot is the right behaviour during an outage,
                # but doing it silently is not: the operator sees a published version
                # the gateway is not running and has no way to tell. This is the only
                # place that knows, so it has to say so.
                log_event(
                    logger,
                    logging.WARNING,
                    "configuration_refresh_failed_serving_cached",
                    error_type=type(exc).__name__,
                    cached_version=self._snapshot.version,
                )
            return self.snapshot

def stranded_models(payload: dict[str, Any]) -> list[str]:
    """Enabled models that would have no usable route in this snapshot.

    A model is usable when at least one enabled provider-model belongs to an
    enabled provider and at least one enabled credential is permitted to serve it.
    Deliberately ignores transient runtime health: a temporarily rate-limited
    credential must not make a configuration unpublishable.
    """
    providers = {
        str(provider.get("id")): provider
        for provider in payload.get("providers") or []
    }
    credentials = [
        credential
        for credential in payload.get("credentials") or []
        if credential.get("enabled")
    ]
    usable: set[str] = set()
    for mapping in payload.get("provider_models") or []:
        if not mapping.get("enabled"):
            continue
        provider = providers.get(str(mapping.get("provider_id")))
        if provider is None or not provider.get("enabled"):
            continue
        allowed = mapping.get("allowed_credential_ids")
        if allowed is not None and len(allowed) == 0:
            # An empty pool means "no credential may serve this route".
            continue
        allowed_set = {str(item) for item in allowed} if allowed is not None else None
        for credential in credentials:
            if str(credential.get("provider_id")) != str(mapping.get("provider_id")):
                continue
            if allowed_set is not None and str(credential.get("id")) not in allowed_set:
                continue
            supported = credential.get("supported_provider_model_ids") or []
            if supported and str(mapping.get("id")) not in {str(x) for x in supported}:
                continue
            usable.add(str(mapping.get("canonical_model_id")))
            break
    return sorted(
        str(model.get("id"))
        for model in payload.get("models") or []
        if model.get("enabled") and str(model.get("id")) not in usable
    )
