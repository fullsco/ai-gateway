import base64
import json
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from gateway.admin.auth import AdminClaims
from gateway.app import create_app
from gateway.auth import authenticate_key
from gateway.config import Settings
from gateway.configuration import (
    CachedConfiguration,
    ConfigSnapshot,
    RuntimeBuilder,
    RuntimeManager,
)
from gateway.security import CredentialCipher, EncryptedCredential, GatewayKeyHasher


class AdminVerifier:
    async def verify(self, token: str) -> AdminClaims:
        assert token == "admin-session"
        return AdminClaims(subject="00000000-0000-0000-0000-000000000001", email=None, role="admin")


class Transaction(AbstractAsyncContextManager[None]):
    def __init__(self, **options: Any) -> None:
        self.options = options

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: object) -> None:
        return None


class ConnectionContext(AbstractAsyncContextManager[Any]):
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    async def __aenter__(self) -> Any:
        return self.connection

    async def __aexit__(self, *args: object) -> None:
        return None


class FakePool:
    def __init__(self) -> None:
        self.fetchrow_result: dict[str, Any] | None = None
        self.fetchrow_results: list[dict[str, Any] | None] = []
        self.fetchval_result: Any = None
        self.fetch_results: list[list[dict[str, Any]]] = []
        self.fetchrow_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.transaction_options: list[dict[str, Any]] = []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.fetchrow_calls.append((query, args))
        if self.fetchrow_results:
            return self.fetchrow_results.pop(0)
        return self.fetchrow_result

    async def fetchval(self, query: str, *args: Any) -> Any:
        return self.fetchval_result

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if self.fetch_results:
            return self.fetch_results.pop(0)
        return []

    async def execute(self, query: str, *args: Any) -> None:
        self.execute_calls.append((query, args))

    def acquire(self) -> ConnectionContext:
        return ConnectionContext(self)

    def transaction(self, **options: Any) -> Transaction:
        self.transaction_options.append(options)
        return Transaction(**options)


class SnapshotPool(FakePool):
    def __init__(self) -> None:
        super().__init__()
        self.fetch_queries: list[str] = []

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.fetch_queries.append(query)
        return await super().fetch(query, *args)


def settings() -> Settings:
    return Settings(
        environment="test",
        credential_encryption_key=base64.b64encode(b"e" * 32).decode(),
        key_pepper=base64.b64encode(b"p" * 32).decode(),
        _env_file=None,
    )


def client(pool: FakePool) -> TestClient:
    return TestClient(create_app(settings(), admin_verifier=AdminVerifier(), db_pool=pool))


def auth() -> dict[str, str]:
    return {"authorization": "Bearer admin-session"}


def test_control_plane_mutations_require_authentication() -> None:
    pool = FakePool()

    with client(pool) as test_client:
        response = test_client.post(
            "/api/admin/v1/providers",
            json={
                "name": "primary",
                "provider_type": "anthropic_compatible",
                "protocol": "anthropic_messages",
                "base_url": "https://example.com",
            },
        )

    assert response.status_code == 401
    assert pool.fetchrow_calls == []


def test_provider_creation_allows_shared_provider_without_protocol_metadata() -> None:
    pool = FakePool()
    pool.fetchrow_result = {
        "id": "provider-id",
        "name": "Shared provider",
        "provider_type": None,
        "protocol": None,
        "base_url": "https://provider.example",
        "enabled": True,
        "priority": 100,
        "capabilities": [],
        "timeout_seconds": 600,
        "settings": {},
        "health": "healthy",
    }

    with client(pool) as test_client:
        response = test_client.post(
            "/api/admin/v1/providers",
            headers=auth(),
            json={"name": "Shared provider", "base_url": "https://provider.example"},
        )

    assert response.status_code == 201
    assert pool.fetchrow_calls[0][1][1:4] == (None, None, "https://provider.example")


def test_credential_creation_encrypts_secret_and_returns_only_masked_hint() -> None:
    pool = FakePool()
    pool.fetchrow_result = {
        "id": "credential-id",
        "provider_id": "provider-id",
        "name": "primary",
        "masked_hint": "sk-t...7890",
        "enabled": True,
        "priority": 100,
        "health": "healthy",
        "quota_limit": None,
        "quota_used": 0,
        "cooldown_until": None,
    }
    secret = "sk-test-secret-1234567890"

    with client(pool) as test_client:
        response = test_client.post(
            "/api/admin/v1/credentials",
            headers=auth(),
            json={"provider_id": "provider-id", "name": "primary", "secret": secret},
        )

    assert response.status_code == 201
    assert secret not in response.text
    assert "secret_ciphertext" not in response.text
    insert_args = pool.fetchrow_calls[0][1]
    envelope = EncryptedCredential(
        version=insert_args[3],
        nonce=insert_args[4],
        ciphertext=insert_args[5],
    )
    decrypted = CredentialCipher.from_base64(settings().credential_encryption_key or "").decrypt(
        envelope,
        context=f"provider-credential:{insert_args[0]}",
    )
    assert decrypted == secret
    assert pool.execute_calls[-1][1][1:] == (
        "credential_created",
        "credential",
        insert_args[0],
    )


def test_short_credential_secret_has_non_revealing_hint() -> None:
    pool = FakePool()
    pool.fetchrow_result = {
        "id": "credential-id",
        "provider_id": "provider-id",
        "name": "primary",
        "masked_hint": "****",
        "enabled": True,
        "priority": 100,
        "health": "healthy",
        "quota_limit": None,
        "quota_used": 0,
        "cooldown_until": None,
    }

    with client(pool) as test_client:
        response = test_client.post(
            "/api/admin/v1/credentials",
            headers=auth(),
            json={"provider_id": "provider-id", "name": "primary", "secret": "short"},
        )

    assert response.status_code == 201
    assert pool.fetchrow_calls[0][1][6] == "****"
    assert "short" not in response.text


def test_provider_update_preserves_settings_when_omitted() -> None:
    pool = FakePool()
    pool.fetchrow_result = {
        "id": "provider-id",
        "name": "AgentRouter",
        "provider_type": "openai_compatible",
        "protocol": "openai_chat_completions",
        "base_url": "https://agentrouter.org",
        "enabled": True,
        "priority": 100,
        "capabilities": ["streaming"],
        "timeout_seconds": 600,
        "settings": {"default_headers": {"originator": "codex_cli_rs"}},
        "health": "healthy",
    }

    with client(pool) as test_client:
        response = test_client.put(
            "/api/admin/v1/providers/provider-id",
            headers=auth(),
            json={
                "name": "AgentRouter",
                "provider_type": "openai_compatible",
                "protocol": "openai_chat_completions",
                "base_url": "https://agentrouter.org",
                "capabilities": ["streaming"],
            },
        )

    assert response.status_code == 200
    query, args = pool.fetchrow_calls[0]
    assert "case when $10::boolean then $11::jsonb else settings end" in query
    assert args[9] is False


def test_gateway_key_is_stored_as_digest_and_returned_once() -> None:
    pool = FakePool()
    pool.fetchval_result = 1

    with client(pool) as test_client:
        response = test_client.post(
            "/api/admin/v1/clients/client-id/keys",
            headers=auth(),
        )

    assert response.status_code == 201
    plaintext = response.json()["key"]
    key_insert = pool.execute_calls[0][1]
    assert plaintext.startswith("gw_live_")
    assert plaintext not in key_insert
    assert key_insert[2] == plaintext[:16]
    digest = GatewayKeyHasher.from_base64(settings().key_pepper or "").digest(plaintext)
    assert digest == key_insert[3]
    assert all(plaintext not in call[1] for call in pool.execute_calls)


def test_publishing_empty_valid_snapshot_is_transactional_and_audited() -> None:
    pool = SnapshotPool()
    pool.fetch_results = [[], [], [], [], [], []]
    pool.fetchrow_result = {"id": 7, "published_at": datetime(2026, 8, 13, tzinfo=UTC)}

    with client(pool) as test_client:
        response = test_client.post("/api/admin/v1/config/publish", headers=auth())

    assert response.status_code == 201
    assert response.json()["version"] == 7
    assert len(response.json()["checksum"]) == 64
    assert pool.transaction_options == [{"isolation": "repeatable_read"}]
    assert "pg_advisory_xact_lock" in pool.execute_calls[0][0]
    assert "status='superseded'" in pool.execute_calls[1][0]
    assert "insert into public.config_versions" in pool.fetchrow_calls[0][0]
    assert pool.execute_calls[-1][1][1:] == (
        "config_published",
        "config_version",
        "7",
    )
    provider_model_query = next(
        query for query in pool.fetch_queries if "from public.provider_models" in query
    )
    assert "join public.model_routes" in provider_model_query
    assert "where pm.enabled and r.enabled" in provider_model_query
    assert "r.priority" in provider_model_query
    assert "max_concurrency" in provider_model_query
    client_query = next(
        query for query in pool.fetch_queries if "from public.gateway_clients" in query
    )
    assert "requests_per_minute" in client_query
    assert "tokens_per_minute" in client_query
    assert "spending_limit" in client_query
    provider_query = next(query for query in pool.fetch_queries if "from public.providers" in query)
    assert "settings->'default_headers'" in provider_query
    assert "settings->>'auth_scheme'" in provider_query
    assert "settings->'endpoint_query'" in provider_query
    key_query = next(query for query in pool.fetch_queries if "gateway_client_keys" in query)
    assert "expires_at" in key_query
    credential_query = next(
        query for query in pool.fetch_queries if "from public.provider_credentials" in query
    )
    assert "credential_model_access" in credential_query
    assert "supported_provider_model_ids" in credential_query


def test_rollback_publishes_new_monotonic_snapshot_and_audits_atomically() -> None:
    pool = SnapshotPool()
    payload = {
        "clients": [],
        "gateway_keys": [],
        "providers": [],
        "credentials": [],
        "models": [],
        "provider_models": [],
    }
    canonical = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    checksum = __import__("hashlib").sha256(canonical.encode()).hexdigest()
    pool.fetchrow_results = [
        {"id": 3, "schema_version": 1, "payload": payload, "checksum": checksum},
        {"id": 8, "published_at": datetime(2026, 8, 15, tzinfo=UTC)},
    ]

    with client(pool) as test_client:
        response = test_client.post(
            "/api/admin/v1/config/versions/3/rollback", headers=auth()
        )

    assert response.status_code == 200
    assert response.json()["version"] == 8
    assert response.json()["source_version"] == 3
    assert "insert into public.config_versions" in pool.fetchrow_calls[1][0]
    assert not any(
        "set status='published'" in query for query, _ in pool.execute_calls
    )
    assert pool.execute_calls[-1][1][1:] == (
        "config_rolled_back",
        "config_version",
        "8",
    )


def test_publishing_decodes_provider_json_settings_from_asyncpg() -> None:
    pool = SnapshotPool()
    pool.fetch_results = [
        [],
        [],
        [
            {
                "id": "provider-id",
                "name": "AgentRouter",
                "provider_type": "anthropic_compatible",
                "protocol": "anthropic_messages",
                "base_url": "https://agentrouter.org",
                "enabled": True,
                "capabilities": ["streaming"],
                "timeout_seconds": 600,
                "health": "healthy",
                "default_headers": '{"user-agent":"claude-cli/test"}',
                "required_betas": '["claude-code-20250219"]',
                "auth_scheme": "bearer",
                "endpoint_query": '{"beta":"true"}',
            }
        ],
        [],
        [],
        [],
    ]
    pool.fetchrow_result = {"id": 8, "published_at": datetime(2026, 8, 14, tzinfo=UTC)}

    with client(pool) as test_client:
        response = test_client.post("/api/admin/v1/config/publish", headers=auth())

    assert response.status_code == 201
    insert_args = next(
        args
        for query, args in pool.fetchrow_calls
        if "insert into public.config_versions" in query
    )
    provider = json.loads(insert_args[0])["providers"][0]
    assert provider["default_headers"] == {"user-agent": "claude-cli/test"}
    assert provider["required_betas"] == ["claude-code-20250219"]
    assert provider["endpoint_query"] == {"beta": "true"}


def test_client_rejects_unknown_protocol_before_database_write() -> None:
    pool = FakePool()

    with client(pool) as test_client:
        response = test_client.post(
            "/api/admin/v1/clients",
            headers=auth(),
            json={"name": "OpenCode", "allowed_protocols": ["openai_chat"]},
        )

    assert response.status_code == 422
    assert pool.fetchrow_calls == []


def test_publish_returns_structured_validation_error() -> None:
    pool = SnapshotPool()
    pool.fetch_results = [
        [
            {
                "id": "client-id",
                "name": "OpenCode",
                "enabled": True,
                "allowed_protocols": ["openai_chat"],
                "allowed_models": [],
            }
        ],
        [],
        [],
        [],
        [],
        [],
        [],
    ]

    with client(pool) as test_client:
        response = test_client.post("/api/admin/v1/config/publish", headers=auth())

    assert response.status_code == 422
    assert response.json()["error"] == "configuration_validation_failed"
    assert response.json()["details"][0]["location"] == "clients.0.allowed_protocols.0"
    assert not any(
        "insert into public.config_versions" in query for query, _ in pool.fetchrow_calls
    )


@pytest.mark.asyncio
async def test_publish_refresh_authenticates_issued_gateway_key() -> None:
    app_settings = settings()
    issued = GatewayKeyHasher.from_base64(app_settings.key_pepper or "").issue(
        key_id="key-id",
        client_id="client-id",
    )
    pool = SnapshotPool()
    pool.fetch_results = [
        [
            {
                "id": "client-id",
                "name": "Claude Code",
                "enabled": True,
                "allowed_protocols": ["anthropic_messages"],
                "allowed_models": [],
            }
        ],
        [
            {
                "id": issued.record.id,
                "client_id": issued.record.client_id,
                "key_prefix": issued.record.key_prefix,
                "key_digest": issued.record.digest,
                "enabled": True,
            }
        ],
        [],
        [],
        [],
        [],
        [],
    ]
    pool.fetchrow_result = {"id": 8, "published_at": datetime(2026, 8, 14, tzinfo=UTC)}

    with client(pool) as test_client:
        response = test_client.post("/api/admin/v1/config/publish", headers=auth())

    assert response.status_code == 201
    insert_args = next(
        args
        for query, args in pool.fetchrow_calls
        if "insert into public.config_versions" in query
    )
    payload = json.loads(insert_args[0])
    snapshot = ConfigSnapshot.create(
        version=8,
        payload=payload,
        published_at=datetime(2026, 8, 14, tzinfo=UTC),
    )

    class PublishedRepository:
        async def load_published(self):
            return snapshot

    manager = RuntimeManager(
        CachedConfiguration(PublishedRepository()),
        RuntimeBuilder(
            encryption_key=app_settings.credential_encryption_key or "",
            key_pepper=app_settings.key_pepper or "",
        ),
    )
    runtime = await manager.refresh()

    assert manager.version == 8
    assert runtime is not None
    authenticated = authenticate_key(
        {"authorization": f"Bearer {issued.plaintext}"},
        store=runtime.key_store,
        hasher=runtime.key_hasher,
    )
    assert authenticated is not None
    assert authenticated.client.id == "client-id"
    await manager.close()
