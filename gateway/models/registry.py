from collections.abc import Mapping
from dataclasses import dataclass

from gateway.protocols import Capability, ClientProtocol, NormalizedRequest


@dataclass(frozen=True)
class CanonicalModel:
    id: str
    aliases: frozenset[str]
    capabilities: frozenset[Capability]
    enabled: bool = True


@dataclass(frozen=True)
class ProviderModel:
    id: str
    canonical_model_id: str
    provider_id: str
    upstream_model_id: str
    protocol: ClientProtocol
    capabilities: frozenset[Capability]
    priority: int = 100
    weight: float = 1.0
    enabled: bool = True
    routing_policy: Mapping[str, object] | None = None
    max_concurrency: int = 8
    pricing: Mapping[str, object] | None = None
    route_id: str | None = None
    provider_name: str | None = None
    allowed_credential_ids: frozenset[str] | None = None
    pool_members: Mapping[str, object] | None = None
    pool_strategy: str | None = None
    allow_model_fallback: bool = True
    # Which client APIs this route answers. `protocol` is what the gateway speaks to
    # the provider; anything here that differs from it is served by translating the
    # request, the response and the SSE stream.
    serves_protocols: frozenset[ClientProtocol] = frozenset()

    def __post_init__(self) -> None:
        # Resolved once, here, so every reader sees the same answer: an empty set means
        # the upstream protocol only, and a route built before this field existed keeps
        # exactly the reachability it had.
        if not self.serves_protocols:
            object.__setattr__(self, "serves_protocols", frozenset({self.protocol}))


class ModelRegistry:
    def __init__(
        self,
        models: list[CanonicalModel],
        provider_models: list[ProviderModel],
    ) -> None:
        self._models = {model.id: model for model in models}
        self._aliases: dict[str, str] = {}
        for model in models:
            for name in {model.id, *model.aliases}:
                normalized = self._normalize_name(name)
                existing = self._aliases.get(normalized)
                if existing is not None and existing != model.id:
                    raise ValueError(f"Model alias {name!r} is assigned more than once")
                self._aliases[normalized] = model.id
        self._provider_models = tuple(provider_models)
        for provider_model in provider_models:
            if provider_model.canonical_model_id not in self._models:
                raise ValueError(
                    f"Provider model {provider_model.id!r} references an unknown canonical model"
                )
            if provider_model.weight <= 0:
                raise ValueError("Provider model weight must be greater than zero")

    def resolve(self, requested_model: str) -> CanonicalModel:
        model_id = self._aliases.get(self._normalize_name(requested_model))
        if model_id is None:
            raise LookupError(f"Unknown model: {requested_model}")
        model = self._models[model_id]
        if not model.enabled:
            raise LookupError(f"Model is disabled: {requested_model}")
        return model

    def list_enabled(self) -> tuple[CanonicalModel, ...]:
        enabled = (model for model in self._models.values() if model.enabled)
        return tuple(sorted(enabled, key=lambda model: model.id))

    def list_provider_models(self) -> tuple[ProviderModel, ...]:
        return tuple(route for route in self._provider_models if route.enabled)

    def eligible_provider_models(self, request: NormalizedRequest) -> tuple[ProviderModel, ...]:
        model = self.resolve(request.requested_model)
        unsupported = request.required_capabilities - model.capabilities
        if unsupported:
            names = ", ".join(sorted(capability.value for capability in unsupported))
            raise LookupError(f"Model does not support required capabilities: {names}")

        eligible = [
            route
            for route in self._provider_models
            if route.enabled
            and route.canonical_model_id == model.id
            # Not `route.protocol is request.protocol`: a route may answer a client
            # protocol its upstream does not speak, which the gateway serves by
            # translating. Requiring identity here is what made every OpenAI-protocol
            # mapping return 404 to a client that speaks Anthropic Messages.
            and request.protocol in route.serves_protocols
            and request.required_capabilities <= route.capabilities
        ]
        return tuple(sorted(eligible, key=lambda route: (route.priority, route.id)))

    @staticmethod
    def _normalize_name(value: str) -> str:
        return value.strip().lower()
