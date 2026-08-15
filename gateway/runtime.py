from dataclasses import dataclass, field

import httpx

from gateway.auth import GatewayKeyStore
from gateway.models import ModelRegistry
from gateway.providers import Credential, ProviderAdapter
from gateway.routing import CredentialState, ProviderState, RouteControls, RoutingEngine
from gateway.security.gateway_keys import GatewayKeyHasher


@dataclass(frozen=True)
class GatewayRuntime:
    key_store: GatewayKeyStore
    key_hasher: GatewayKeyHasher
    model_registry: ModelRegistry
    routing_engine: RoutingEngine
    provider_states: tuple[ProviderState, ...]
    credential_states: tuple[CredentialState, ...]
    provider_model_adapters: dict[str, ProviderAdapter]
    credentials: dict[str, Credential]
    http_client: httpx.AsyncClient | None
    route_controls: RouteControls = field(default_factory=RouteControls)
