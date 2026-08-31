from typing import Any

from pydantic import AnyHttpUrl, BaseModel, Field, field_validator, model_validator

from gateway.admin.control_plane import (
    NormalizedStringLists,
    ProviderModelInput,
    normalize_served_protocols,
)
from gateway.protocols import Capability, ClientProtocol


def normalized_name(value: str) -> str:
    return value.strip().casefold()


def enum_value(value: Any) -> str:
    return str(getattr(value, "value", value))


class ReconcileCredential(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    secret: str | None = Field(default=None, min_length=1, repr=False)
    rotate_secret: bool = False
    enabled: bool = True
    priority: int = Field(default=100, ge=0)
    quota_limit: float | None = Field(default=None, ge=0)
    quota_threshold: float = Field(default=0.95, gt=0, le=1)
    requests_per_minute: int | None = Field(default=None, gt=0)
    tokens_per_minute: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_rotation(self) -> "ReconcileCredential":
        if self.rotate_secret and self.secret is None:
            raise ValueError("rotate_secret requires secret")
        if self.secret is not None and not self.rotate_secret:
            self.rotate_secret = True
        return self


class ReconcileModel(NormalizedStringLists):
    id: str = Field(min_length=1, max_length=160)
    display_name: str | None = Field(default=None, min_length=1, max_length=160)
    aliases: list[str] = Field(default_factory=list)
    capabilities: list[Capability] = Field(default_factory=list)
    enabled: bool | None = None
    context_window: int | None = Field(default=None, gt=0)


class ReconcileMapping(NormalizedStringLists):
    model_id: str
    upstream_model_id: str = Field(min_length=1, max_length=240)
    protocol: ClientProtocol
    serves_protocols: list[ClientProtocol] = Field(default_factory=list)
    capabilities: list[Capability] = Field(default_factory=list)
    enabled: bool = True
    priority: int = Field(default=100, ge=0)
    weight: float = Field(default=1, gt=0)
    max_concurrency: int = Field(default=8, gt=0)
    settings: dict[str, Any] = Field(default_factory=dict)
    pricing: dict[str, Any] = Field(default_factory=dict)

    @field_validator("pricing")
    @classmethod
    def validate_pricing(cls, pricing: dict[str, Any]) -> dict[str, Any]:
        return ProviderModelInput.validate_pricing(pricing)

    @model_validator(mode="after")
    def normalize_served(self) -> "ReconcileMapping":
        self.serves_protocols = normalize_served_protocols(self.protocol, self.serves_protocols)
        return self


class ReconcileRoute(BaseModel):
    model_id: str
    mapping_upstream_model_id: str
    mapping_protocol: ClientProtocol
    priority: int = Field(default=100, ge=0)
    enabled: bool = True
    allow_model_fallback: bool = False


class ProviderReconcileInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    base_url: AnyHttpUrl
    enabled: bool = True
    priority: int = Field(default=100, ge=0)
    timeout_seconds: float = Field(default=600, gt=0)
    settings: dict[str, Any] | None = None
    credentials: list[ReconcileCredential] = Field(default_factory=list)
    models: list[ReconcileModel] = Field(default_factory=list)
    mappings: list[ReconcileMapping] = Field(default_factory=list)
    routes: list[ReconcileRoute] = Field(default_factory=list)
    pool_strategy: str = "priority"
    pool_enabled: bool = True
    health_aware: bool = True
    quota_aware: bool = True

    @field_validator("pool_strategy")
    @classmethod
    def valid_pool_strategy(cls, value: str) -> str:
        if value not in {"priority", "weighted", "least_loaded"}:
            raise ValueError("pool_strategy must be priority, weighted, or least_loaded")
        return value

    @model_validator(mode="after")
    def validate_graph(self) -> "ProviderReconcileInput":
        credential_names = [credential.name for credential in self.credentials]
        if len(set(credential_names)) != len(credential_names):
            raise ValueError("credential names must be unique")
        model_ids = [model.id for model in self.models]
        normalized_model_ids = [normalized_name(model_id) for model_id in model_ids]
        if len(set(normalized_model_ids)) != len(normalized_model_ids):
            raise ValueError("model ids must be unique")
        normalized_aliases = [
            normalized_name(alias) for model in self.models for alias in model.aliases
        ]
        if len(set(normalized_aliases)) != len(normalized_aliases):
            raise ValueError("model aliases must be unique after normalization")
        model_owners = dict(zip(normalized_model_ids, model_ids, strict=True))
        for model in self.models:
            for alias in model.aliases:
                owner = model_owners.get(normalized_name(alias))
                if owner is not None and owner != model.id:
                    raise ValueError(f"model alias {alias} conflicts with model id {owner}")
        mapping_keys = [
            (mapping.model_id, mapping.upstream_model_id, mapping.protocol)
            for mapping in self.mappings
        ]
        if len(set(mapping_keys)) != len(mapping_keys):
            raise ValueError("mappings must be unique")
        models = {model.id: set(model.capabilities) for model in self.models}
        for mapping in self.mappings:
            if mapping.model_id not in models:
                raise ValueError(f"mapping references unknown model {mapping.model_id}")
            if not set(mapping.capabilities) <= models[mapping.model_id]:
                raise ValueError(
                    f"mapping capabilities must be supported by model {mapping.model_id}"
                )
        route_keys = [
            (route.model_id, route.mapping_upstream_model_id, route.mapping_protocol)
            for route in self.routes
        ]
        if len(set(route_keys)) != len(route_keys):
            raise ValueError("routes must be unique")
        available_mappings = set(mapping_keys)
        for route, key in zip(self.routes, route_keys, strict=True):
            if key not in available_mappings:
                raise ValueError(f"route references unknown mapping for model {route.model_id}")
        return self
