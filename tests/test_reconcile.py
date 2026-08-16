import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from gateway.admin.reconcile import ProviderReconcileInput, ReconcileMapping
from gateway.app import create_app
from tests.reconcile_support import (
    AdminVerifier,
    ReconcilePool,
    app_settings,
    auth,
    reconcile_app,
    reconcile_payload,
)


def test_reconcile_rejects_duplicate_normalized_aliases_and_routes() -> None:
    with pytest.raises(ValidationError, match="aliases must be unique after normalization"):
        ProviderReconcileInput.model_validate(
            reconcile_payload(
                models=[
                    {
                        "id": "model-1",
                        "display_name": "Model 1",
                        "aliases": [" Latest ", "latest"],
                        "capabilities": ["streaming"],
                    }
                ]
            )
        )

    route = reconcile_payload()["routes"][0]
    with pytest.raises(ValidationError, match="routes must be unique"):
        ProviderReconcileInput.model_validate(reconcile_payload(routes=[route, route]))


def test_reconcile_mapping_reuses_provider_model_pricing_validation() -> None:
    mapping = ReconcileMapping(
        model_id="model-1",
        upstream_model_id="upstream-1",
        protocol="openai_responses",
        pricing={"input_per_million": 1, "output_per_million": 2, "currency": "eur"},
    )
    assert mapping.pricing["currency"] == "EUR"

    with pytest.raises(ValidationError, match="nonnegative and finite"):
        ReconcileMapping(
            model_id="model-1",
            upstream_model_id="upstream-1",
            protocol="openai_responses",
            pricing={
                "input_per_million": -1,
                "output_per_million": 2,
                "currency": "USD",
            },
        )


def test_reconcile_validates_encryption_key_before_provider_write() -> None:
    pool = ReconcilePool()
    settings = app_settings(encryption_key="invalid-base64")
    with TestClient(
        reconcile_app(pool, settings),
        raise_server_exceptions=False,
    ) as test_client:
        response = test_client.put(
            "/api/admin/v1/providers/reconcile",
            headers=auth(),
            json=reconcile_payload(),
        )

    assert response.status_code == 503
    assert response.json() == {"error": "credential_encryption_not_configured"}
    assert pool.fetchrow_calls == []
    assert pool.execute_calls == []
    assert pool.transactions[-1].outcome == "rolled_back"


def test_reconcile_short_secret_uses_non_revealing_masked_hint() -> None:
    pool = ReconcilePool()
    with TestClient(
        reconcile_app(pool),
        raise_server_exceptions=False,
    ) as test_client:
        response = test_client.put(
            "/api/admin/v1/providers/reconcile",
            headers=auth(),
            json=reconcile_payload(
                credentials=[{"name": "primary", "secret": "short"}],
                models=[],
                mappings=[],
                routes=[],
            ),
        )

    assert response.status_code == 200
    credential_insert = next(
        args
        for query, args in pool.execute_calls
        if "insert into public.provider_credentials" in query
    )
    assert credential_insert[6] == "****"
    assert "short" not in response.text


def test_reconcile_redacts_secrets_from_validation_and_audit_metadata() -> None:
    pool = ReconcilePool()
    secret = "validation-secret-value"
    with TestClient(reconcile_app(pool), raise_server_exceptions=False) as test_client:
        response = test_client.put(
            "/api/admin/v1/providers/reconcile",
            headers=auth(),
            json=reconcile_payload(
                credentials=[{"name": "primary", "secret": secret}],
                settings={"safe": True},
            ),
        )

    assert response.status_code == 200
    assert secret not in response.text
    audit_args = next(
        args for query, args in pool.execute_calls if "insert into public.audit_logs" in query
    )
    assert secret not in repr(audit_args)


def test_reconcile_validation_response_does_not_echo_secret_input() -> None:
    pool = ReconcilePool()
    secret = "must-not-appear-in-validation"
    with TestClient(
        create_app(app_settings(), admin_verifier=AdminVerifier(), db_pool=pool),
        raise_server_exceptions=False,
    ) as test_client:
        response = test_client.put(
            "/api/admin/v1/providers/reconcile",
            headers=auth(),
            json=reconcile_payload(
                credentials=[
                    {
                        "name": "primary",
                        "secret": secret,
                        "quota_threshold": 2,
                    }
                ]
            ),
        )

    assert response.status_code == 422
    assert secret not in response.text
    assert pool.execute_calls == []
    assert pool.transactions[-1].outcome == "rolled_back"


def test_reconcile_custom_validation_error_is_json_serializable() -> None:
    pool = ReconcilePool()
    with TestClient(
        create_app(app_settings(), admin_verifier=AdminVerifier(), db_pool=pool),
        raise_server_exceptions=False,
    ) as test_client:
        response = test_client.put(
            "/api/admin/v1/providers/reconcile",
            headers=auth(),
            json=reconcile_payload(routes=[reconcile_payload()["routes"][0]] * 2),
        )

    assert response.status_code == 422
    assert response.json()["detail"][0]["ctx"]["error"] == "routes must be unique"
    assert pool.transactions[-1].outcome == "rolled_back"


def test_reconcile_rejects_unsupported_existing_topology_before_writes() -> None:
    pool = ReconcilePool(
        existing_provider_id="provider-id",
        topology_conflict="selective_credential_access",
    )
    with TestClient(reconcile_app(pool), raise_server_exceptions=False) as test_client:
        response = test_client.put(
            "/api/admin/v1/providers/reconcile",
            headers=auth(),
            json=reconcile_payload(),
        )

    assert response.status_code == 409
    assert response.json() == {
        "error": "provider_topology_not_supported",
        "reason": "selective_credential_access",
    }
    assert pool.fetchrow_calls == []
    assert pool.transactions[-1].outcome == "rolled_back"


def test_reconcile_pool_ownership_conflict_rolls_back_late_writes() -> None:
    pool = ReconcilePool(conflicting_pool_provider="other-provider")
    with TestClient(reconcile_app(pool), raise_server_exceptions=False) as test_client:
        response = test_client.put(
            "/api/admin/v1/providers/reconcile",
            headers=auth(),
            json=reconcile_payload(
                credentials=[{"name": "primary", "secret": "late-conflict-secret"}],
            ),
        )

    assert response.status_code == 409
    assert response.json() == {
        "error": "provider_pool_ownership_conflict",
        "pool_name": "Provider Pool",
    }
    assert any("insert into public.providers" in query for query, _ in pool.fetchrow_calls)
    assert any(
        "insert into public.provider_credentials" in query
        for query, _ in pool.execute_calls
    )
    assert not any("insert into public.audit_logs" in query for query, _ in pool.execute_calls)
    assert pool.transactions[-1].outcome == "rolled_back"


def test_reconcile_repeated_requests_use_idempotent_sql_keys_and_preserve_settings() -> None:
    pool = ReconcilePool()
    payload = reconcile_payload(
        settings={"region": "east", "custom": {"keep": True}},
        models=[
            {
                "id": "model-1",
                "display_name": "Preserved Model",
                "aliases": ["latest"],
                "capabilities": ["streaming"],
                "context_window": 128000,
            }
        ],
    )
    with TestClient(reconcile_app(pool)) as test_client:
        first = test_client.put(
            "/api/admin/v1/providers/reconcile", headers=auth(), json=payload
        )
        second = test_client.put(
            "/api/admin/v1/providers/reconcile", headers=auth(), json=payload
        )

    assert first.status_code == second.status_code == 200
    statements = "\n".join(query for query, _ in pool.fetchrow_calls + pool.execute_calls)
    assert statements.count("on conflict(name) do update") >= 2
    assert "on conflict(provider_id,model_id,upstream_model_id,protocol) do update" in statements
    assert "on conflict(alias) do nothing" in statements
    provider_args = next(
        args for query, args in pool.fetchrow_calls if "insert into public.providers" in query
    )
    assert provider_args[-1] == '{"region": "east", "custom": {"keep": true}}'


def test_reconcile_rejects_existing_alias_owner_without_mutation() -> None:
    pool = ReconcilePool(alias_rows=[{"alias": "LATEST", "model_id": "other-model"}])
    with TestClient(
        reconcile_app(pool),
        raise_server_exceptions=False,
    ) as test_client:
        response = test_client.put(
            "/api/admin/v1/providers/reconcile",
            headers=auth(),
            json=reconcile_payload(),
        )

    assert response.status_code == 409
    assert response.json() == {
        "error": "model_alias_ownership_conflict",
        "alias": "LATEST",
        "existing_model_id": "other-model",
        "requested_model_id": "model-1",
    }
    assert pool.fetchrow_calls == []
    assert not any("insert into public.model_aliases" in query for query, _ in pool.execute_calls)
    assert pool.transactions[-1].outcome == "rolled_back"


def test_reconcile_rejects_alias_matching_existing_canonical_model() -> None:
    pool = ReconcilePool(namespace_model_rows=[{"id": "latest"}])
    with TestClient(reconcile_app(pool), raise_server_exceptions=False) as test_client:
        response = test_client.put(
            "/api/admin/v1/providers/reconcile",
            headers=auth(),
            json=reconcile_payload(
                models=[
                    {
                        "id": "model-1",
                        "display_name": "Model 1",
                        "aliases": ["LATEST"],
                        "capabilities": ["streaming"],
                    }
                ]
            ),
        )

    assert response.status_code == 409
    assert response.json()["error"] == "model_namespace_conflict"
    assert pool.fetchrow_calls == []


def test_reconcile_rejects_shared_model_metadata_changes() -> None:
    pool = ReconcilePool(
        existing_provider_id="provider-id",
        shared_model_rows=[
            {
                "id": "model-1",
                "display_name": "Original",
                "enabled": True,
                "capabilities": ["streaming"],
                "context_window": 128000,
            }
        ],
    )
    with TestClient(reconcile_app(pool), raise_server_exceptions=False) as test_client:
        response = test_client.put(
            "/api/admin/v1/providers/reconcile",
            headers=auth(),
            json=reconcile_payload(
                models=[
                    {
                        "id": "model-1",
                        "display_name": "Changed",
                        "capabilities": ["streaming"],
                    }
                ]
            ),
        )

    assert response.status_code == 409
    assert response.json() == {
        "error": "shared_model_metadata_conflict",
        "model_id": "model-1",
    }


def test_reconcile_route_is_reachable_in_production_app() -> None:
    pool = ReconcilePool()
    with TestClient(
        create_app(app_settings(), admin_verifier=AdminVerifier(), db_pool=pool),
        raise_server_exceptions=False,
    ) as test_client:
        response = test_client.put(
            "/api/admin/v1/providers/reconcile",
            headers=auth(),
            json=reconcile_payload(models=[], mappings=[], routes=[]),
        )

    assert response.status_code == 200
    assert response.json()["provider_id"] == "provider-id"


def test_reconcile_soft_disables_omissions_and_replaces_access() -> None:
    pool = ReconcilePool()
    with TestClient(reconcile_app(pool), raise_server_exceptions=False) as test_client:
        response = test_client.put(
            "/api/admin/v1/providers/reconcile",
            headers=auth(),
            json=reconcile_payload(models=[], mappings=[], routes=[]),
        )

    assert response.status_code == 200
    statements = "\n".join(query for query, _ in pool.execute_calls)
    assert "update public.provider_credentials" in statements
    assert "update public.provider_models" in statements
    assert "delete from public.credential_model_access" in statements
    assert "update public.provider_pool_members" in statements
    assert "update public.model_routes" in statements


def test_reconcile_persists_pool_member_priority_and_weight() -> None:
    pool = ReconcilePool()
    with TestClient(reconcile_app(pool), raise_server_exceptions=False) as test_client:
        response = test_client.put(
            "/api/admin/v1/providers/reconcile",
            headers=auth(),
            json=reconcile_payload(
                credentials=[{"name": "primary", "secret": "secret-value", "priority": 7}],
                mappings=[
                    {
                        "model_id": "model-1",
                        "upstream_model_id": "upstream-1",
                        "protocol": "anthropic_messages",
                        "capabilities": ["streaming"],
                        "weight": 3,
                    }
                ],
            ),
        )

    assert response.status_code == 200
    member_args = next(
        args
        for query, args in pool.execute_calls
        if "insert into public.provider_pool_members" in query
    )
    assert member_args[3:] == (7, 3.0)
