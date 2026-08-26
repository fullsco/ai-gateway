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
                "shared_with": ["Other Provider"],
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
    # Which field differs is asserted by
    # test_shared_model_conflict_names_the_field_that_differs.
    assert response.json()["error"] == "shared_model_metadata_conflict"
    assert response.json()["model_id"] == "model-1"


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


def test_absent_credential_access_is_unrestricted_not_selective() -> None:
    """No access rows means no restriction, so it must not block reconciling.

    The topology guard refuses to reconcile a provider whose credentials are
    deliberately restricted to particular mappings, because reconciling would flatten
    that. But it decided this by looking for pairs that lack an access row, and a
    provider with no access rows at all has every pair lacking one. That is the
    opposite condition: the router treats an empty restriction set as "may serve
    anything", and every provider created before this flow existed has zero rows.

    The effect in production was that adding a model or a credential to hcnsec,
    GoRouter, TabiAi or AgentRouter returned provider_topology_not_supported forever,
    because a complete access matrix is only ever written when a provider is first
    created. Confirmed against the live database: hcnsec, with one credential, one
    mapping and zero access rows, was refused.

    Both selective clauses must therefore be gated on the relevant rows existing.
    """
    from gateway.admin.reconcile_guards import _TOPOLOGY_QUERY

    query = " ".join(_TOPOLOGY_QUERY.split())

    access_clause = query.index("then 'selective_credential_access'")
    access_condition = query[:access_clause]
    assert "from public.credential_model_access cma join provider_credentials c" in (
        access_condition
    ), "selective_credential_access must first require that some access row exists"

    membership_clause = query.index("then 'selective_pool_membership'")
    membership_condition = query[access_clause:membership_clause]
    assert "from public.provider_pool_members ppm join managed_pool mp on mp.id=ppm.pool_id)" in (
        membership_condition
    ), "selective_pool_membership must first require that some pool member exists"

    # The genuinely selective case must still be detectable, so the pair check stays.
    assert query.count("cross join provider_mappings pm where not exists") == 2


def test_shared_model_conflict_names_the_field_that_differs() -> None:
    """A refusal has to say what is in the way, or it cannot be acted on.

    The guard is right to refuse: this model is served by another provider too, and a
    per-provider form must not silently rewrite catalogue-wide metadata. But answering
    with only the model id left the operator guessing which of display name, enabled,
    capabilities, context window or aliases was the problem -- and the setup form
    pre-fills display_name from the id and leaves context_window blank, so it invites
    exactly the edit it then rejects.
    """
    pool = ReconcilePool(
        existing_provider_id="provider-id",
        shared_model_rows=[
            {
                "id": "model-1",
                "display_name": "Original",
                "enabled": True,
                "capabilities": ["streaming"],
                "context_window": 128000,
                "shared_with": ["Other Provider"],
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
    body = response.json()
    assert body["error"] == "shared_model_metadata_conflict"
    assert body["model_id"] == "model-1"
    # Which field, what it is now, and what the form tried to make it.
    assert body["field"] == "display_name"
    assert body["current"] == "Original"
    assert body["requested"] == "Changed"
    # And who else depends on it, so the operator knows why it is shared.
    assert body["shared_with"] == ["Other Provider"]


def test_reconcile_disables_catalogue_models_left_without_a_live_route() -> None:
    pool = ReconcilePool(
        previously_mapped_models=["deepseek-v4-flash"],
        sync_disabled_ids=["deepseek-v4-flash"],
    )
    with TestClient(reconcile_app(pool), raise_server_exceptions=False) as test_client:
        response = test_client.put(
            "/api/admin/v1/providers/reconcile",
            headers=auth(),
            json=reconcile_payload(models=[], mappings=[], routes=[]),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["models_disabled"] == ["deepseek-v4-flash"]
    assert body["models_enabled"] == []
    sync_query, sync_args = next(
        (query, args)
        for query, args in pool.fetch_calls
        if "update public.models m set enabled=false" in query
    )
    assert "not exists" in sync_query
    assert list(sync_args[0]) == ["deepseek-v4-flash"]
    audit_args = next(
        args
        for query, args in pool.execute_calls
        if "provider_reconciled" in query
    )
    metadata = __import__("json").loads(audit_args[2])
    assert metadata["models_disabled"] == ["deepseek-v4-flash"]


def test_reconcile_reenables_a_stranded_model_when_a_route_returns() -> None:
    pool = ReconcilePool(
        previously_mapped_models=["deepseek-v4-flash"],
        sync_enabled_ids=["deepseek-v4-flash"],
    )
    with TestClient(reconcile_app(pool), raise_server_exceptions=False) as test_client:
        response = test_client.put(
            "/api/admin/v1/providers/reconcile",
            headers=auth(),
            json=reconcile_payload(),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["models_enabled"] == ["deepseek-v4-flash"]
    assert body["models_disabled"] == []


def test_reconcile_only_judges_models_this_save_touches() -> None:
    pool = ReconcilePool()
    with TestClient(reconcile_app(pool), raise_server_exceptions=False) as test_client:
        response = test_client.put(
            "/api/admin/v1/providers/reconcile",
            headers=auth(),
            json=reconcile_payload(),
        )

    assert response.status_code == 200
    _, disable_args = next(
        (query, args)
        for query, args in pool.fetch_calls
        if "update public.models m set enabled=false" in query
    )
    _, enable_args = next(
        (query, args)
        for query, args in pool.fetch_calls
        if "update public.models m set enabled=true" in query
    )
    # The affected set is the provider's previous mappings plus declared ones.
    assert sorted(disable_args[0]) == ["model-1"]
    assert sorted(enable_args[0]) == ["model-1"]
