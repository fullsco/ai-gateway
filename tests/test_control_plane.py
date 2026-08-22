import base64
import json
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from gateway.admin.auth import AdminClaims
from gateway.admin.control_plane import (
    ClientKeyInput,
    ModelRoutingInput,
    ProviderModelInput,
    _snapshot_payload,
)
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


def test_client_key_expiry_must_be_in_the_future() -> None:
    with pytest.raises(ValidationError, match="expires_at must be in the future"):
        ClientKeyInput(expires_at=datetime(2020, 1, 1, tzinfo=UTC))


def test_model_routing_requires_one_primary_and_unique_providers() -> None:
    with pytest.raises(ValidationError, match="at least one primary provider"):
        ModelRoutingInput(providers=[{"provider": "GoRouter", "fallback": True}])

    with pytest.raises(ValidationError, match="providers must be unique"):
        ModelRoutingInput(
            providers=[
                {"provider": "AgentRouter"},
                {"provider": "agentr ou ter".replace(" ", "")},
            ]
        )


class AdminVerifier:
    async def verify(self, token: str) -> AdminClaims:
        assert token == "admin-session"
        return AdminClaims(subject="00000000-0000-0000-0000-000000000001", email=None, role="admin")


class Transaction(AbstractAsyncContextManager[None]):
    def __init__(self, **options: Any) -> None:
        self.options = options
        self.outcome: str | None = None

    async def start(self) -> None:
        self.outcome = "started"

    async def commit(self) -> None:
        self.outcome = "committed"

    async def rollback(self) -> None:
        self.outcome = "rolled_back"

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
        self.transactions: list[Transaction] = []

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
        transaction = Transaction(**options)
        self.transactions.append(transaction)
        return transaction


class SnapshotPool(FakePool):
    """Answers snapshot queries by what they ask for, not by call order.

    A positional queue was consumed by the background live-state refresh before
    publish ever ran, so the canned rows arrived at the wrong queries. The rows are
    keyed on a distinguishing fragment of each snapshot query instead.
    """

    # Order matters: the provider_models query also selects a provider's
    # default_headers, so it must be matched before the providers query.
    SECTIONS = (
        ("keys", "from public.gateway_client_keys"),
        ("clients", "from public.gateway_clients"),
        ("provider_models", "from public.provider_models pm"),
        ("providers", "settings->'default_headers'"),
        ("credentials", "from public.provider_credentials pc"),
        ("aliases", "from public.model_aliases"),
        ("models", "from public.models"),
    )

    def __init__(self) -> None:
        super().__init__()
        self.fetch_queries: list[str] = []
        self.sections: dict[str, list[dict[str, Any]]] = {}

    def section(self, name: str, rows: list[dict[str, Any]]) -> "SnapshotPool":
        self.sections[name] = rows
        return self

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.fetch_queries.append(query)
        if not self.sections:
            # Tests that call _snapshot_payload directly still queue results in
            # order, which is safe because nothing else is querying the pool.
            return await super().fetch(query, *args)
        for name, marker in self.SECTIONS:
            if marker in query:
                return self.sections.get(name, [])
        # Anything else, such as the live-state refresh, is not part of a snapshot.
        return []

    def query_for(self, section: str) -> str:
        """The snapshot query for a section, matched precisely.

        Selecting by "from public.providers" alone now finds the live-state query
        first, which is a different statement entirely.
        """
        marker = dict(self.SECTIONS)[section]
        return next(query for query in self.fetch_queries if marker in query)


class RequestDetailPool(FakePool):
    def __init__(self) -> None:
        super().__init__()
        self.attempt_query = ""

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        if "from public.request_logs where id=$1" in query:
            return {
                "id": "request-1",
                "requested_model": "claude-opus-5",
                "status": "succeeded",
            }
        return await super().fetchrow(query, *args)

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if "from public.request_attempts a" in query:
            self.attempt_query = query
            return [{
                "id": "attempt-1",
                "attempt_number": 1,
                "provider_name": "AgentRouter",
                "credential_name": "Primary",
                "status": "succeeded",
            }]
        if "from public.usage_records where request_id=$1" in query:
            return []
        return []


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


def test_provider_model_pricing_is_normalized_and_validated() -> None:
    body = ProviderModelInput(
        provider_id="provider-1",
        model_id="model-1",
        upstream_model_id="upstream-1",
        protocol="anthropic_messages",
        pricing={"input_per_million": 1, "output_per_million": 2, "currency": "eur"},
    )
    assert body.pricing["currency"] == "EUR"

    with pytest.raises(ValueError, match="nonnegative"):
        ProviderModelInput(
            provider_id="provider-1",
            model_id="model-1",
            upstream_model_id="upstream-1",
            protocol="anthropic_messages",
            pricing={"input_per_million": -1, "output_per_million": 2, "currency": "USD"},
        )


@pytest.mark.asyncio
async def test_snapshot_decodes_provider_model_pricing_json() -> None:
    pool = SnapshotPool()
    pool.fetch_results = [
        [], [], [], [], [], [],
        [{
            "id": "mapping-1",
            "route_id": "route-1",
            "canonical_model_id": "model-1",
            "provider_id": "provider-1",
            "upstream_model_id": "upstream-1",
            "protocol": "anthropic_messages",
            "capabilities": [],
            "priority": 1,
            "weight": 1,
            "max_concurrency": 8,
            "pricing": '{"input_per_million":1,"output_per_million":2,"currency":"USD"}',
            "allow_model_fallback": True,
            "pool_id": "pool-1",
            "pool_enabled": True,
            "active_pool_credential_ids": ["credential-active"],
            "active_pool_members": {"credential-active": {"priority": 1, "weight": 2}},
            "pool_strategy": "priority",
            "enabled": True,
            "routing_policy": None,
        }],
    ]

    payload = await _snapshot_payload(pool)

    assert payload["provider_models"][0]["pricing"]["currency"] == "USD"
    assert payload["provider_models"][0]["allow_model_fallback"] is True
    assert payload["provider_models"][0]["allowed_credential_ids"] == ["credential-active"]
    assert payload["provider_models"][0]["pool_members"] == {
        "credential-active": {"priority": 1, "weight": 2}
    }
    provider_model_query = pool.fetch_queries[-1]
    assert "ppm.enabled and not ppm.draining" in provider_model_query
    assert "left join public.provider_pools" in provider_model_query


@pytest.mark.asyncio
async def test_snapshot_decodes_pool_members_json_from_asyncpg() -> None:
    pool = SnapshotPool()
    pool.fetch_results = [
        [], [], [], [], [], [],
        [{
            "id": "mapping-1",
            "route_id": "route-1",
            "canonical_model_id": "model-1",
            "provider_id": "provider-1",
            "upstream_model_id": "upstream-1",
            "protocol": "anthropic_messages",
            "capabilities": [],
            "priority": 1,
            "weight": 1,
            "max_concurrency": 8,
            "pricing": "{}",
            "allow_model_fallback": False,
            "pool_id": "pool-1",
            "pool_enabled": True,
            "active_pool_credential_ids": ["credential-active"],
            "active_pool_members": '{"credential-active": {"priority": 1, "weight": 2}}',
            "pool_strategy": "priority",
            "enabled": True,
            "routing_policy": None,
        }],
    ]

    payload = await _snapshot_payload(pool)

    members = payload["provider_models"][0]["pool_members"]
    # Regression: jsonb_object_agg returns a JSON string that must be decoded to a dict,
    # otherwise RuntimeSnapshot validation (config/status and publish) fails.
    assert isinstance(members, dict)
    assert members == {"credential-active": {"priority": 1, "weight": 2}}


@pytest.mark.asyncio
@pytest.mark.parametrize("pool_enabled", [False, None])
async def test_snapshot_disabled_or_missing_pool_fails_closed(pool_enabled: bool | None) -> None:
    pool = SnapshotPool()
    pool.fetch_results = [
        [], [], [], [], [], [],
        [{
            "id": "mapping-1",
            "route_id": "route-1",
            "canonical_model_id": "model-1",
            "provider_id": "provider-1",
            "upstream_model_id": "upstream-1",
            "protocol": "anthropic_messages",
            "capabilities": [],
            "priority": 1,
            "weight": 1,
            "max_concurrency": 8,
            "pricing": {},
            "allow_model_fallback": False,
            "pool_id": "pool-1",
            "pool_enabled": pool_enabled,
            "active_pool_credential_ids": ["credential-must-not-leak"],
            "active_pool_members": {"credential-must-not-leak": {}},
            "pool_strategy": None,
            "enabled": True,
            "routing_policy": None,
        }],
    ]

    payload = await _snapshot_payload(pool)

    route = payload["provider_models"][0]
    assert route["allowed_credential_ids"] == []
    assert route["pool_members"] == {}


@pytest.mark.asyncio
async def test_snapshot_enabled_empty_pool_has_no_eligible_credentials() -> None:
    pool = SnapshotPool()
    pool.fetch_results = [
        [], [], [], [], [], [],
        [{
            "id": "mapping-1",
            "route_id": "route-1",
            "canonical_model_id": "model-1",
            "provider_id": "provider-1",
            "upstream_model_id": "upstream-1",
            "protocol": "anthropic_messages",
            "capabilities": [],
            "priority": 1,
            "weight": 1,
            "max_concurrency": 8,
            "pricing": {},
            "allow_model_fallback": False,
            "pool_id": "pool-1",
            "pool_enabled": True,
            "active_pool_credential_ids": [],
            "active_pool_members": {},
            "pool_strategy": "priority",
            "enabled": True,
            "routing_policy": None,
        }],
    ]

    payload = await _snapshot_payload(pool)

    assert payload["provider_models"][0]["allowed_credential_ids"] == []


class AnalyticsPool(FakePool):
    """Analytics issues ~10 fetches, so results must be keyed on the query.

    A positional queue silently handed the provider rows to costs_by_currency,
    which meant the test could not detect an analytics regression at all.
    """

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if "sum(estimated_cost)" in query and "group by currency" in query:
            return [
                {
                    "currency": "EUR",
                    "estimated_cost": 1.5,
                    "succeeded_cost": 1.0,
                    "failed_cost": 0.5,
                },
                {
                    "currency": "USD",
                    "estimated_cost": 2.5,
                    "succeeded_cost": 2.5,
                    "failed_cost": None,
                },
            ]
        if "canonical_model_snapshot" in query:
            return [{"model": "model-1", "usage_records": 2, "failed_records": 1}]
        if "provider_name_snapshot" in query:
            return [{"provider": "Provider One", "usage_records": 2, "failed_records": 1}]
        if "route_id_snapshot" in query:
            return [{"route": "route-1", "usage_records": 2, "failed_records": 0}]
        if "from public.request_attempts" in query:
            return []
        if "requested_model" in query:
            return [{"model": "model-1", "requests": 2, "succeeded": 1, "failed": 1}]
        if "as day" in query or "date_trunc" in query:
            return [{"day": "2026-08-16", "requests": 2, "succeeded": 1, "failed": 1}]
        return []


def test_analytics_separates_currency_and_uses_immutable_attribution() -> None:
    pool = AnalyticsPool()
    pool.fetchrow_result = {
        "input_tokens": 12,
        "output_tokens": 4,
        "cached_tokens": 2,
        "priced_records": 1,
        "usage_records": 2,
        "failed_records": 1,
        "failed_input_tokens": 6,
        "failed_output_tokens": 1,
    }

    with TestClient(
        create_app(settings(), admin_verifier=AdminVerifier(), db_pool=pool),
        raise_server_exceptions=False,
    ) as test_client:
        response = test_client.get("/api/admin/v1/analytics", headers=auth())

    assert response.status_code == 200
    payload = response.json()
    assert payload["costs_by_currency"] == [
        {
            "currency": "EUR",
            "estimated_cost": 1.5,
            "succeeded_cost": 1.0,
            "failed_cost": 0.5,
        },
        {
            "currency": "USD",
            "estimated_cost": 2.5,
            "succeeded_cost": 2.5,
            "failed_cost": None,
        },
    ]
    assert payload["usage_by_model"][0]["model"] == "model-1"
    assert payload["usage_by_provider"][0]["provider"] == "Provider One"
    assert payload["usage_by_route"][0]["route"] == "route-1"
    # Spend that bought nothing must be visible, not folded into the total.
    assert payload["usage"]["failed_records"] == 1
    assert payload["usage_by_provider"][0]["failed_records"] == 1
    # Cost is never summed across currencies in an attribution query.
    attribution_queries = [query for query, _ in pool.fetchrow_calls]
    assert all("sum(estimated_cost)" not in query for query in attribution_queries)


def test_request_detail_includes_operator_facing_attempt_names() -> None:
    pool = RequestDetailPool()
    test_settings = settings().model_copy(update={"database_url": None})

    with TestClient(
        create_app(test_settings, admin_verifier=AdminVerifier(), db_pool=pool),
        raise_server_exceptions=False,
    ) as test_client:
        response = test_client.get("/api/admin/v1/requests/request-1", headers=auth())

    assert response.status_code == 200
    assert response.json()["attempts"][0]["provider_name"] == "AgentRouter"
    assert response.json()["attempts"][0]["credential_name"] == "Primary"
    assert "left join public.providers" in pool.attempt_query
    assert "left join public.provider_credentials" in pool.attempt_query


class AuditPool(FakePool):
    def __init__(self) -> None:
        super().__init__()
        self.fetch_query = ""

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.fetch_query = query
        return [
            {
                "id": 1,
                "action": "model_routing_updated",
                "resource_type": "model",
                "resource_id": "claude-opus-5",
                "resource_name": "Claude Opus 5",
                "metadata": {"providers": ["AgentRouter"]},
            }
        ]


def test_audit_resolves_human_readable_resource_names() -> None:
    pool = AuditPool()
    test_settings = settings().model_copy(update={"database_url": None})

    with TestClient(
        create_app(test_settings, admin_verifier=AdminVerifier(), db_pool=pool),
        raise_server_exceptions=False,
    ) as test_client:
        response = test_client.get("/api/admin/v1/audit", headers=auth())

    assert response.status_code == 200
    assert response.json()["data"][0]["resource_name"] == "Claude Opus 5"
    assert "as resource_name" in pool.fetch_query
    assert "left join public.provider_models pm" in pool.fetch_query
    assert "'(no longer present)'" in pool.fetch_query


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


def test_pool_members_must_share_a_provider() -> None:
    pool = FakePool()
    pool.fetchrow_result = {
        "id": "00000000-0000-0000-0000-000000000010",
        "name": "Restricted",
    }
    pool.fetchval_result = None

    with TestClient(
        create_app(settings(), admin_verifier=AdminVerifier(), db_pool=pool),
        raise_server_exceptions=False,
    ) as test_client:
        response = test_client.post(
            "/api/admin/v1/provider-pools",
            headers=auth(),
            json={
                "name": "Restricted",
                "members": [
                    {
                        "provider_model_id": "00000000-0000-0000-0000-000000000011",
                        "credential_id": "00000000-0000-0000-0000-000000000012",
                    }
                ],
            },
        )

    assert response.status_code == 422
    assert "share a provider" in response.json()["detail"]
    assert pool.transactions[-1].outcome == "rolled_back"


def test_mutation_and_audit_commit_in_one_transaction() -> None:
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

    with TestClient(
        create_app(settings(), admin_verifier=AdminVerifier(), db_pool=pool),
        raise_server_exceptions=False,
    ) as test_client:
        response = test_client.post(
            "/api/admin/v1/providers",
            headers=auth(),
            json={"name": "Shared provider", "base_url": "https://provider.example"},
        )

    assert response.status_code == 201
    assert pool.transactions[-1].outcome == "committed"
    metadata = json.loads(pool.execute_calls[-1][1][4])
    assert metadata["changed_fields"] == ["base_url", "name"]


def test_audit_failure_rolls_back_mutation_transaction() -> None:
    class AuditFailingPool(FakePool):
        async def execute(self, query: str, *args: Any) -> None:
            if "insert into public.audit_logs" in query:
                raise RuntimeError("audit unavailable")
            await super().execute(query, *args)

    pool = AuditFailingPool()
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

    with TestClient(
        create_app(settings(), admin_verifier=AdminVerifier(), db_pool=pool),
        raise_server_exceptions=False,
    ) as test_client:
        response = test_client.post(
            "/api/admin/v1/providers",
            headers=auth(),
            json={"name": "Shared provider", "base_url": "https://provider.example"},
        )

    assert response.status_code == 500
    assert pool.transactions[-1].outcome == "rolled_back"


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
    assert pool.execute_calls[-1][1][1:4] == (
        "credential_created",
        "credential",
        insert_args[0],
    )


def test_credential_creation_persists_rate_limits_with_matching_parameters() -> None:
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
    with client(pool) as test_client:
        response = test_client.post(
            "/api/admin/v1/credentials",
            headers=auth(),
            json={
                "provider_id": "provider-id",
                "name": "primary",
                "secret": "secret-provider-key",
                "requests_per_minute": 20,
                "tokens_per_minute": 10000,
            },
        )
    assert response.status_code == 201
    query, args = pool.fetchrow_calls[0]
    assert "$12" in query
    assert len(args) == 12
    assert args[-2:] == (20, 10000)


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
    assert pool.execute_calls[-1][1][1:4] == (
        "config_published",
        "config_version",
        "7",
    )
    provider_model_query = pool.query_for("provider_models")
    assert "join public.model_routes" in provider_model_query
    assert "where pm.enabled and r.enabled" in provider_model_query
    assert "r.priority" in provider_model_query
    assert "max_concurrency" in provider_model_query
    client_query = pool.query_for("clients")
    assert "requests_per_minute" in client_query
    assert "tokens_per_minute" in client_query
    assert "spending_limit" in client_query
    provider_query = pool.query_for("providers")
    assert "settings->'default_headers'" in provider_query
    assert "settings->>'auth_scheme'" in provider_query
    assert "settings->'endpoint_query'" in provider_query
    key_query = next(query for query in pool.fetch_queries if "gateway_client_keys" in query)
    assert "expires_at" in key_query
    credential_query = pool.query_for("credentials")
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
    assert pool.execute_calls[-1][1][1:4] == (
        "config_rolled_back",
        "config_version",
        "8",
    )


def test_publishing_decodes_provider_json_settings_from_asyncpg() -> None:
    pool = SnapshotPool()
    pool.section(
        "providers",
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
    )
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
    pool.section(
        "clients",
        [
            {
                "id": "client-id",
                "name": "OpenCode",
                "enabled": True,
                "allowed_protocols": ["openai_chat"],
                "allowed_models": [],
            }
        ],
    )

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
    pool.section(
        "clients",
        [
            {
                "id": "client-id",
                "name": "Claude Code",
                "enabled": True,
                "allowed_protocols": ["anthropic_messages"],
                "allowed_models": [],
            }
        ],
    ).section(
        "keys",
        [
            {
                "id": issued.record.id,
                "client_id": issued.record.client_id,
                "key_prefix": issued.record.key_prefix,
                "key_digest": issued.record.digest,
                "enabled": True,
            }
        ],
    )
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


def test_pricing_accepts_a_measured_blended_rate_with_provenance() -> None:
    body = ProviderModelInput(
        provider_id="provider-1",
        model_id="model-1",
        upstream_model_id="upstream-1",
        protocol="anthropic_messages",
        pricing={
            "blended_per_million": "6.307692",
            "currency": "usd",
            "pricing_basis": "measured_blended",
            "sample_count": 1,
            "confidence": "low",
        },
    )

    assert body.pricing["currency"] == "USD"
    assert body.pricing["pricing_basis"] == "measured_blended"
    assert body.pricing["sample_count"] == 1
    assert body.pricing["confidence"] == "low"


def test_pricing_defaults_basis_and_confidence_for_a_listed_rate_card() -> None:
    body = ProviderModelInput(
        provider_id="provider-1",
        model_id="model-1",
        upstream_model_id="upstream-1",
        protocol="anthropic_messages",
        pricing={"input_per_million": 3, "output_per_million": 15, "currency": "USD"},
    )

    assert body.pricing["pricing_basis"] == "listed"
    assert body.pricing["confidence"] == "high"


def test_pricing_rejects_mixing_blended_and_separated_rates() -> None:
    # The wording changed from "not both" when a third shape, a flat per-request fee,
    # was added: with three mutually exclusive shapes "both" no longer describes it.
    with pytest.raises(ValidationError, match="exactly one of"):
        ProviderModelInput(
            provider_id="provider-1",
            model_id="model-1",
            upstream_model_id="upstream-1",
            protocol="anthropic_messages",
            pricing={
                "blended_per_million": 6,
                "input_per_million": 3,
                "output_per_million": 15,
                "currency": "USD",
            },
        )


def test_blended_pricing_must_declare_its_measured_basis() -> None:
    with pytest.raises(ValidationError, match="measured_blended"):
        ProviderModelInput(
            provider_id="provider-1",
            model_id="model-1",
            upstream_model_id="upstream-1",
            protocol="anthropic_messages",
            pricing={
                "blended_per_million": 6,
                "currency": "USD",
                "pricing_basis": "listed",
            },
        )


def test_a_key_can_be_named_without_exposing_its_secret() -> None:
    """A key could be created, revoked or rotated, but never named.

    Keys issued without a label stayed anonymous, so an operator could not tell
    which client or purpose each one served.
    """
    pool = FakePool()
    pool.fetchrow_result = {
        "id": "key-1",
        "client_id": "client-1",
        "key_prefix": "gw_live_abcd",
        "label": "opencode primary",
        "enabled": True,
        "created_at": None,
    }
    test_settings = settings().model_copy(update={"database_url": None})

    with TestClient(
        create_app(test_settings, admin_verifier=AdminVerifier(), db_pool=pool),
        raise_server_exceptions=False,
    ) as test_client:
        response = test_client.patch(
            "/api/admin/v1/client-keys/key-1",
            headers=auth(),
            json={"label": "opencode primary"},
        )

    assert response.status_code == 200
    assert response.json()["label"] == "opencode primary"
    query, args = pool.fetchrow_calls[0]
    assert "set label=$2" in query
    # The statement touches nothing else, so no secret can be altered by a rename.
    assert "key_digest" not in query
    assert args == ("key-1", "opencode primary")


def test_naming_an_unknown_key_is_a_not_found() -> None:
    pool = FakePool()
    pool.fetchrow_result = None
    test_settings = settings().model_copy(update={"database_url": None})

    with TestClient(
        create_app(test_settings, admin_verifier=AdminVerifier(), db_pool=pool),
        raise_server_exceptions=False,
    ) as test_client:
        response = test_client.patch(
            "/api/admin/v1/client-keys/missing", headers=auth(), json={"label": "x"}
        )

    assert response.status_code == 404
