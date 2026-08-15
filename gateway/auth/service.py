from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from gateway.protocols import ClientProtocol
from gateway.security.gateway_keys import GatewayKey, GatewayKeyHasher


@dataclass(frozen=True)
class ClientPermissions:
    protocols: frozenset[ClientProtocol]
    allowed_models: frozenset[str] = frozenset()

    def permits(self, protocol: ClientProtocol, model: str) -> bool:
        protocol_allowed = protocol in self.protocols
        model_allowed = not self.allowed_models or model in self.allowed_models
        return protocol_allowed and model_allowed


@dataclass(frozen=True)
class GatewayClient:
    id: str
    name: str
    permissions: ClientPermissions
    enabled: bool = True
    requests_per_minute: int | None = None
    tokens_per_minute: int | None = None
    spending_limit: float | None = None


@dataclass(frozen=True)
class AuthenticatedClient:
    client: GatewayClient
    key_id: str


class GatewayKeyStore(Protocol):
    def get_by_prefix(self, key_prefix: str) -> GatewayKey | None:
        """Look up a candidate key record by a non-secret prefix."""

    def get_client(self, client_id: str) -> GatewayClient | None:
        """Look up the owner of a gateway key."""


class InMemoryGatewayKeyStore:
    def __init__(self, keys: list[GatewayKey], clients: list[GatewayClient]) -> None:
        self._keys = {key.key_prefix: key for key in keys}
        self._clients = {client.id: client for client in clients}

    def get_by_prefix(self, key_prefix: str) -> GatewayKey | None:
        return self._keys.get(key_prefix)

    def get_client(self, client_id: str) -> GatewayClient | None:
        return self._clients.get(client_id)

    def authenticate_token(
        self,
        token: str,
        hasher: GatewayKeyHasher,
    ) -> AuthenticatedClient | None:
        if len(token) < 16:
            return None
        key = self.get_by_prefix(token[:16])
        if key is None or not hasher.verify(token, key):
            return None
        client = self.get_client(key.client_id)
        if client is None or not client.enabled:
            return None
        return AuthenticatedClient(client=client, key_id=key.id)


def extract_gateway_key(headers: Mapping[str, str]) -> str | None:
    normalized = {key.lower(): value for key, value in headers.items()}
    if api_key := normalized.get("x-api-key"):
        return api_key.strip()
    authorization = normalized.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        return token.strip()
    return None


def authenticate_request(
    headers: Mapping[str, str],
    protocol: ClientProtocol,
    requested_model: str,
    *,
    store: GatewayKeyStore,
    hasher: GatewayKeyHasher,
) -> AuthenticatedClient | None:
    token = extract_gateway_key(headers)
    if not token:
        return None
    if isinstance(store, InMemoryGatewayKeyStore):
        authenticated = store.authenticate_token(token, hasher)
    else:
        if len(token) < 16:
            return None
        key = store.get_by_prefix(token[:16])
        if key is None or not hasher.verify(token, key):
            return None
        client = store.get_client(key.client_id)
        authenticated = (
            AuthenticatedClient(client=client, key_id=key.id)
            if client is not None and client.enabled
            else None
        )
    if authenticated is None:
        return None
    if not authenticated.client.permissions.permits(protocol, requested_model):
        return None
    return authenticated


def authenticate_key(
    headers: Mapping[str, str],
    *,
    store: GatewayKeyStore,
    hasher: GatewayKeyHasher,
) -> AuthenticatedClient | None:
    token = extract_gateway_key(headers)
    if not token or len(token) < 16:
        return None
    key = store.get_by_prefix(token[:16])
    if key is None or not hasher.verify(token, key):
        return None
    client = store.get_client(key.client_id)
    if client is None or not client.enabled:
        return None
    return AuthenticatedClient(client=client, key_id=key.id)
