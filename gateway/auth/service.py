from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from gateway.protocols import ClientProtocol
from gateway.security.gateway_keys import GatewayKey, GatewayKeyHasher


class AuthDenial(StrEnum):
    """Why a request was not accepted.

    Authentication and authorization used to collapse into a bare None, so a key
    that was valid but not permitted to use a protocol was reported as "Invalid
    gateway key". That sent operators looking for a bad secret when the real
    problem was the client's allowed_protocols, which is a configuration choice
    they can see and change.
    """

    MISSING_KEY = "missing_key"
    INVALID_KEY = "invalid_key"
    KEY_REVOKED = "key_revoked"
    KEY_EXPIRED = "key_expired"
    CLIENT_DISABLED = "client_disabled"
    PROTOCOL_NOT_PERMITTED = "protocol_not_permitted"
    MODEL_NOT_PERMITTED = "model_not_permitted"


@dataclass(frozen=True)
class ClientPermissions:
    protocols: frozenset[ClientProtocol]
    allowed_models: frozenset[str] = frozenset()

    def permits(self, protocol: ClientProtocol, model: str) -> bool:
        return self.denial(protocol, model) is None

    def denial(self, protocol: ClientProtocol, model: str) -> AuthDenial | None:
        """Which permission is missing, so the caller can say which one."""
        if protocol not in self.protocols:
            return AuthDenial.PROTOCOL_NOT_PERMITTED
        if self.allowed_models and model not in self.allowed_models:
            return AuthDenial.MODEL_NOT_PERMITTED
        return None


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
) -> AuthenticatedClient | AuthDenial:
    """Resolve the caller, or say precisely why the request was not accepted.

    Returning the reason rather than None lets the API distinguish "this secret is
    not a key we know" from "this key is real but its client may not use this
    protocol or model". The caller has already proven possession of the key in the
    second case, so naming the missing permission reveals nothing it does not
    already have, and it is the difference between a five minute fix and an hour
    spent doubting the secret.
    """
    token = extract_gateway_key(headers)
    if not token:
        return AuthDenial.MISSING_KEY
    if len(token) < 16:
        return AuthDenial.INVALID_KEY
    key = store.get_by_prefix(token[:16])
    if key is None:
        return AuthDenial.INVALID_KEY
    # Possession of the secret is established before anything about the key's state
    # is reported, so a prefix alone reveals nothing.
    if not hasher.matches(token, key):
        return AuthDenial.INVALID_KEY
    if not key.enabled:
        return AuthDenial.KEY_REVOKED
    if key.expires_at is not None and key.expires_at <= datetime.now(UTC):
        return AuthDenial.KEY_EXPIRED
    client = store.get_client(key.client_id)
    if client is None:
        return AuthDenial.INVALID_KEY
    if not client.enabled:
        return AuthDenial.CLIENT_DISABLED
    denial = client.permissions.denial(protocol, requested_model)
    if denial is not None:
        return denial
    return AuthenticatedClient(client=client, key_id=key.id)


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
