import base64
import binascii
import logging
from datetime import datetime
from typing import Any, Literal

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator, model_validator

from gateway.auth import ClientPermissions, GatewayClient, InMemoryGatewayKeyStore
from gateway.logging import log_event
from gateway.models import CanonicalModel, ModelRegistry, ProviderModel
from gateway.protocols import Capability, ClientProtocol
from gateway.protocols.translate import get_translator
from gateway.providers import Credential, ProviderConfig, TranslatingAdapter
from gateway.providers.anthropic import AnthropicCompatibleAdapter
from gateway.providers.egress import build_upstream_client
from gateway.providers.openai import OpenAICompatibleAdapter
from gateway.routing import (
    CredentialState,
    HealthState,
    ProviderState,
    RouteControls,
    RoutingEngine,
)
from gateway.runtime import GatewayRuntime
from gateway.security import CredentialCipher, EncryptedCredential, GatewayKey, GatewayKeyHasher

# Snapshot models ignore fields they do not recognise rather than rejecting them.
# These payloads are produced by the control plane, not by hand, so an unknown field
# means the publisher is newer than this reader, not that someone mistyped something.
# Rejecting it is catastrophic and silent: a newer field made every older instance
# refuse the whole snapshot and freeze on its last good configuration. One did, for a
# day, serving version 242 while 249 was published, because "timeout_seconds" was
# added to provider_models. Fifteen thousand validation errors and no behaviour
# change. Ignoring the field instead means an older instance keeps working, without
# the feature it cannot understand, which is what a rolling deployment needs.
#
# Unknown fields are reported by report_unknown_snapshot_fields so this degrades
# visibly rather than quietly.
logger = logging.getLogger("gateway.configuration.runtime")


class SnapshotClient(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    enabled: bool = True
    allowed_protocols: frozenset[ClientProtocol]
    allowed_models: frozenset[str] = frozenset()
    requests_per_minute: int | None = Field(default=None, gt=0)
    tokens_per_minute: int | None = Field(default=None, gt=0)
    spending_limit: float | None = Field(default=None, ge=0)


class SnapshotGatewayKey(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    client_id: str
    key_prefix: str
    key_digest: str
    enabled: bool = True
    expires_at: datetime | None = None


class SnapshotProvider(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    provider_type: Literal["anthropic_compatible", "openai_compatible"] | None = None
    protocol: ClientProtocol | None = None
    base_url: AnyHttpUrl
    enabled: bool = True
    capabilities: frozenset[Capability]
    timeout_seconds: float = Field(default=600, gt=0)
    health: HealthState = HealthState.HEALTHY
    circuit_open: bool = False
    latency_ms: float = Field(default=0, ge=0)
    failure_rate: float = Field(default=0, ge=0, le=1)
    priority: int = Field(default=100, ge=0)
    default_headers: dict[str, str] = Field(default_factory=dict)
    required_betas: frozenset[str] = frozenset()
    auth_scheme: Literal["default", "bearer", "x-api-key", "both"] = "default"
    endpoint_query: dict[str, str] = Field(default_factory=dict)

    @field_validator("default_headers")
    @classmethod
    def reject_credential_headers(
        cls, headers: dict[str, str] | None
    ) -> dict[str, str] | None:
        if headers is None:
            return None
        forbidden = {"authorization", "cookie", "proxy-authorization", "x-api-key"}
        rejected = sorted(name for name in headers if name.lower() in forbidden)
        if rejected:
            raise ValueError(
                f"default_headers may not contain credential headers: {', '.join(rejected)}"
            )
        return headers


class SnapshotCredential(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    provider_id: str
    secret_version: int = 1
    secret_nonce: str
    secret_ciphertext: str
    enabled: bool = True
    priority: int = 100
    health: HealthState = HealthState.HEALTHY
    quota_limit: float | None = Field(default=None, ge=0)
    quota_used: float = Field(default=0, ge=0)
    rpm_headroom: float = Field(default=1, ge=0, le=1)
    tpm_headroom: float = Field(default=1, ge=0, le=1)
    concurrency_headroom: float = Field(default=1, ge=0, le=1)
    failure_rate: float = Field(default=0, ge=0, le=1)
    latency_ms: float = Field(default=0, ge=0)
    cooldown_until: datetime | None = None
    supported_provider_model_ids: frozenset[str] = frozenset()
    requests_per_minute: int | None = Field(default=None, gt=0)
    tokens_per_minute: int | None = Field(default=None, gt=0)


class SnapshotModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    aliases: frozenset[str] = frozenset()
    capabilities: frozenset[Capability]
    enabled: bool = True


class SnapshotProviderModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    canonical_model_id: str
    provider_id: str
    upstream_model_id: str
    protocol: ClientProtocol
    # Client APIs this mapping answers; anything other than `protocol` is served by
    # translating. Empty means the upstream protocol only, which is how every mapping
    # behaved before this field existed and so is what an older snapshot must mean.
    serves_protocols: frozenset[ClientProtocol] = frozenset()
    capabilities: frozenset[Capability]
    priority: int = 100
    weight: float = Field(default=1, gt=0)
    enabled: bool = True
    routing_policy: dict[str, Any] | None = None
    max_concurrency: int = Field(default=8, gt=0)
    default_headers: dict[str, str] | None = None
    required_betas: frozenset[str] | None = None
    auth_scheme: Literal["default", "bearer", "x-api-key", "both"] | None = None
    endpoint_query: dict[str, str] | None = None
    # A per-route ceiling on how long one attempt may take. The provider's timeout is
    # shared by every model it serves, so a single slow route could not be bounded
    # without slowing the rest. hcnsec answers in anywhere from 6 to 600 seconds, and
    # at 600 a stalled attempt holds a slot and the client for ten minutes.
    timeout_seconds: float | None = Field(default=None, gt=0)
    pricing: dict[str, Any] = Field(default_factory=dict)
    route_id: str | None = None
    allowed_credential_ids: frozenset[str] | None = None
    pool_members: dict[str, Any] | None = None
    pool_strategy: str | None = None
    # Snapshots published before route fallback was compiled retain historical behavior.
    allow_model_fallback: bool = True

    @field_validator("default_headers")
    @classmethod
    def reject_credential_headers(
        cls, headers: dict[str, str] | None
    ) -> dict[str, str] | None:
        if headers is None:
            return None
        forbidden = {"authorization", "cookie", "proxy-authorization", "x-api-key"}
        rejected = sorted(name for name in headers if name.lower() in forbidden)
        if rejected:
            raise ValueError(
                "provider-model default_headers may not contain credential headers: "
                f"{', '.join(rejected)}"
            )
        return headers


class RuntimeSnapshot(BaseModel):
    model_config = ConfigDict(extra="ignore")

    clients: list[SnapshotClient]
    gateway_keys: list[SnapshotGatewayKey]
    providers: list[SnapshotProvider]
    credentials: list[SnapshotCredential]
    models: list[SnapshotModel]
    provider_models: list[SnapshotProviderModel]

    @model_validator(mode="after")
    def validate_route_compatibility(self) -> "RuntimeSnapshot":
        providers = {provider.id: provider for provider in self.providers}
        models = {model.id: model for model in self.models}
        for route in self.provider_models:
            provider = providers.get(route.provider_id)
            model = models.get(route.canonical_model_id)
            if provider is None:
                raise ValueError(
                    f"Provider model {route.id!r} references unknown provider {route.provider_id!r}"
                )
            if model is None:
                raise ValueError(
                    f"Provider model {route.id!r} references unknown model "
                    f"{route.canonical_model_id!r}"
                )
            if route.capabilities - model.capabilities:
                raise ValueError(
                    f"Provider model {route.id!r} capabilities must be supported by its "
                    "canonical model"
                )
        return self


_SNAPSHOT_SECTION_MODELS: dict[str, type[BaseModel]] = {
    "clients": SnapshotClient,
    "gateway_keys": SnapshotGatewayKey,
    "providers": SnapshotProvider,
    "credentials": SnapshotCredential,
    "models": SnapshotModel,
    "provider_models": SnapshotProviderModel,
}


def unknown_snapshot_fields(payload: dict[str, Any]) -> dict[str, list[str]]:
    """Fields the publisher sent that this build does not model, by section.

    Ignoring an unknown field keeps an older instance running, but silently ignoring
    it would hide the fact that it is running without something the configuration
    asked for. This names them so the gap is visible.
    """
    found: dict[str, set[str]] = {}
    for section, model in _SNAPSHOT_SECTION_MODELS.items():
        rows = payload.get(section)
        if not isinstance(rows, list):
            continue
        known = set(model.model_fields)
        for row in rows:
            if not isinstance(row, dict):
                continue
            if extra := row.keys() - known:
                found.setdefault(section, set()).update(extra)
    top_level = payload.keys() - set(RuntimeSnapshot.model_fields)
    if top_level:
        found.setdefault("snapshot", set()).update(top_level)
    return {section: sorted(names) for section, names in sorted(found.items())}


def report_unknown_snapshot_fields(payload: dict[str, Any]) -> dict[str, list[str]]:
    """Log any unmodelled fields once, and return them for callers that want them."""
    unknown = unknown_snapshot_fields(payload)
    if unknown:
        log_event(
            logger,
            logging.WARNING,
            "snapshot_has_unmodelled_fields",
            sections={section: ",".join(names) for section, names in unknown.items()},
            hint="this build is older than the configuration publisher",
        )
    return unknown


class RuntimeBuilder:
    def __init__(self, *, encryption_key: str, key_pepper: str) -> None:
        self._cipher = CredentialCipher.from_base64(encryption_key)
        self._key_hasher = GatewayKeyHasher(self._decode_pepper(key_pepper))

    def build(self, payload: dict[str, Any]) -> GatewayRuntime:
        report_unknown_snapshot_fields(payload)
        snapshot = RuntimeSnapshot.model_validate(payload)
        clients = [
            GatewayClient(
                id=client.id,
                name=client.name,
                enabled=client.enabled,
                permissions=ClientPermissions(client.allowed_protocols, client.allowed_models),
                requests_per_minute=client.requests_per_minute,
                tokens_per_minute=client.tokens_per_minute,
                spending_limit=client.spending_limit,
            )
            for client in snapshot.clients
        ]
        keys = [
            GatewayKey(
                id=key.id,
                client_id=key.client_id,
                key_prefix=key.key_prefix,
                digest=key.key_digest,
                enabled=key.enabled,
                expires_at=key.expires_at,
            )
            for key in snapshot.gateway_keys
        ]
        models = [
            CanonicalModel(
                id=model.id,
                aliases=model.aliases,
                capabilities=model.capabilities,
                enabled=model.enabled,
            )
            for model in snapshot.models
        ]
        provider_models = [
            ProviderModel(
                id=model.id,
                canonical_model_id=model.canonical_model_id,
                provider_id=model.provider_id,
                upstream_model_id=model.upstream_model_id,
                protocol=model.protocol,
                capabilities=model.capabilities,
                priority=model.priority,
                weight=model.weight,
                enabled=model.enabled,
                routing_policy=model.routing_policy,
                max_concurrency=model.max_concurrency,
                pricing=model.pricing,
                route_id=model.route_id,
                provider_name=next(
                    provider.name
                    for provider in snapshot.providers
                    if provider.id == model.provider_id
                ),
                allowed_credential_ids=model.allowed_credential_ids,
                pool_members=model.pool_members,
                pool_strategy=model.pool_strategy,
                allow_model_fallback=model.allow_model_fallback,
                serves_protocols=model.serves_protocols,
            )
            for model in snapshot.provider_models
        ]
        registry = ModelRegistry(models, provider_models)
        adapters = {
            model.id: self._build_adapter(provider, model)
            for model in snapshot.provider_models
            if model.enabled
            for provider in snapshot.providers
            if provider.id == model.provider_id and provider.enabled
        }
        translating = {
            (model.id, client_protocol): TranslatingAdapter(adapters[model.id], translator)
            for model in snapshot.provider_models
            if model.id in adapters
            for client_protocol in sorted(model.serves_protocols)
            # The registry covers every ordered pair of protocols, so None here is the
            # identity pair: a protocol served natively, which needs no wrapper.
            if (translator := get_translator(client_protocol, model.protocol)) is not None
        }
        credentials, credential_states = self._build_credentials(snapshot.credentials)
        provider_states = tuple(
            ProviderState(
                provider_id=provider.id,
                health=provider.health,
                enabled=provider.enabled,
                circuit_open=provider.circuit_open,
                latency_ms=provider.latency_ms,
                failure_rate=provider.failure_rate,
                priority=provider.priority,
            )
            for provider in snapshot.providers
        )
        return GatewayRuntime(
            key_store=InMemoryGatewayKeyStore(keys, clients),
            key_hasher=self._key_hasher,
            model_registry=registry,
            routing_engine=RoutingEngine(registry),
            provider_states=provider_states,
            credential_states=credential_states,
            provider_model_adapters=adapters,
            translating_adapters=translating,
            credentials=credentials,
            http_client=build_upstream_client(),
            route_controls=RouteControls(
                {model.id: model.max_concurrency for model in snapshot.provider_models}
            ),
        )

    def _build_adapter(
        self,
        provider: SnapshotProvider,
        model: SnapshotProviderModel,
    ):
        protocol = model.protocol
        capabilities = model.capabilities
        config = ProviderConfig(
            id=provider.id,
            name=provider.name,
            base_url=provider.base_url,
            protocol=protocol,
            capabilities=capabilities,
            # A route may tighten the provider's timeout but not loosen it, so one
            # slow mapping cannot quietly raise the ceiling for everything else.
            timeout_seconds=(
                min(model.timeout_seconds, provider.timeout_seconds)
                if model.timeout_seconds is not None
                else provider.timeout_seconds
            ),
        )
        # A mapping may override a provider-wide setting, including overriding it to
        # nothing: an explicit empty value is a decision, so absence is `is None`
        # rather than falsiness.
        default_headers = (
            model.default_headers if model.default_headers is not None
            else provider.default_headers
        )
        endpoint_query = (
            model.endpoint_query if model.endpoint_query is not None
            else provider.endpoint_query
        )
        if protocol is ClientProtocol.ANTHROPIC_MESSAGES:
            return AnthropicCompatibleAdapter(
                config,
                default_headers=default_headers,
                endpoint_query=endpoint_query,
                required_betas=(
                    model.required_betas
                    if model.required_betas is not None
                    else provider.required_betas
                ),
                auth_scheme=(
                    model.auth_scheme if model.auth_scheme is not None else provider.auth_scheme
                ),
            )
        if protocol not in {
            ClientProtocol.OPENAI_CHAT_COMPLETIONS,
            ClientProtocol.OPENAI_RESPONSES,
        }:
            raise ValueError(f"Unsupported provider-model protocol: {protocol.value}")
        return OpenAICompatibleAdapter(
            config, default_headers=default_headers, endpoint_query=endpoint_query
        )

    def _build_credentials(
        self,
        snapshot_credentials: list[SnapshotCredential],
    ) -> tuple[dict[str, Credential], tuple[CredentialState, ...]]:
        credentials: dict[str, Credential] = {}
        states: list[CredentialState] = []
        for item in snapshot_credentials:
            secret = self._cipher.decrypt(
                EncryptedCredential(
                    version=item.secret_version,
                    nonce=item.secret_nonce,
                    ciphertext=item.secret_ciphertext,
                ),
                context=f"provider-credential:{item.id}",
            )
            credentials[item.id] = Credential(id=item.id, secret=secret)
            quota_headroom = self._quota_headroom(item.quota_limit, item.quota_used)
            states.append(
                CredentialState(
                    credential_id=item.id,
                    provider_id=item.provider_id,
                    health=item.health,
                    enabled=item.enabled,
                    priority=item.priority,
                    quota_headroom=quota_headroom,
                    rpm_headroom=item.rpm_headroom,
                    tpm_headroom=item.tpm_headroom,
                    concurrency_headroom=item.concurrency_headroom,
                    failure_rate=item.failure_rate,
                    latency_ms=item.latency_ms,
                    cooldown_until=item.cooldown_until,
                    supported_provider_model_ids=item.supported_provider_model_ids,
                    requests_per_minute=item.requests_per_minute,
                    tokens_per_minute=item.tokens_per_minute,
                )
            )
        return credentials, tuple(states)

    @staticmethod
    def _quota_headroom(limit: float | None, used: float) -> float:
        if limit is None:
            return 1
        if limit == 0:
            return 0
        return max(0, min(1, (limit - used) / limit))

    @staticmethod
    def _decode_pepper(value: str) -> bytes:
        try:
            decoded = base64.b64decode(value, validate=True)
        except binascii.Error as exc:
            raise ValueError("Gateway key pepper must be valid base64") from exc
        if len(decoded) < 32:
            raise ValueError("Gateway key pepper must decode to at least 32 bytes")
        return decoded
