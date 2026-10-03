from dataclasses import dataclass, field

import httpx

from gateway.auth import GatewayKeyStore
from gateway.models import ModelRegistry
from gateway.protocols import ClientProtocol
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
    # Adapters for the client protocols a mapping serves by translation, keyed by
    # (provider_model_id, client_protocol). Separate from provider_model_adapters
    # because that map answers a different question -- how to talk to this upstream --
    # which is what a health probe needs and what translation is built on top of.
    translating_adapters: dict[tuple[str, ClientProtocol], ProviderAdapter] = field(
        default_factory=dict
    )

    def adapter_for(
        self,
        provider_model_id: str,
        client_protocol: ClientProtocol,
    ) -> ProviderAdapter:
        """The adapter that serves this client protocol on this mapping.

        A translating adapter when the mapping answers a protocol its upstream does not
        speak, and the mapping's own adapter otherwise, so a native route resolves to
        exactly the object it resolved to before translation existed.
        """
        translated = self.translating_adapters.get((provider_model_id, client_protocol))
        if translated is not None:
            return translated
        return self.provider_model_adapters[provider_model_id]
