from datetime import UTC, datetime, timedelta

import pytest

from gateway.models import CanonicalModel, ModelRegistry, ProviderModel
from gateway.protocols import Capability, ClientProtocol, normalize_request
from gateway.routing import CredentialState, HealthState, ProviderState, RoutingEngine
from gateway.routing.engine import NoRouteAvailable


def make_engine() -> RoutingEngine:
    registry = ModelRegistry(
        models=[
            CanonicalModel(
                id="model-x",
                aliases=frozenset({"latest-x"}),
                capabilities=frozenset({Capability.STREAMING}),
            )
        ],
        provider_models=[
            ProviderModel(
                id="a-model-x",
                canonical_model_id="model-x",
                provider_id="provider-a",
                upstream_model_id="upstream-x",
                protocol=ClientProtocol.ANTHROPIC_MESSAGES,
                capabilities=frozenset({Capability.STREAMING}),
                priority=10,
            ),
            ProviderModel(
                id="b-model-x",
                canonical_model_id="model-x",
                provider_id="provider-b",
                upstream_model_id="other-x",
                protocol=ClientProtocol.ANTHROPIC_MESSAGES,
                capabilities=frozenset({Capability.STREAMING}),
                priority=20,
            ),
        ],
    )
    return RoutingEngine(registry)


def make_request():
    return normalize_request(
        ClientProtocol.ANTHROPIC_MESSAGES,
        {"model": "latest-x", "stream": True, "messages": []},
    )


def test_prefers_route_and_credential_priority_before_composite_score() -> None:
    decision = make_engine().select(
        make_request(),
        [ProviderState("provider-a"), ProviderState("provider-b")],
        [
            CredentialState("a-low-usage", "provider-a", priority=20, quota_headroom=1),
            CredentialState("a-priority", "provider-a", priority=10, quota_headroom=0.2),
            CredentialState("b-best", "provider-b", priority=1, quota_headroom=1),
        ],
    )

    assert decision.provider_model.provider_id == "provider-a"
    assert decision.credential.credential_id == "a-priority"
    assert decision.canonical_model_id == "model-x"


def test_model_route_priority_beats_provider_priority() -> None:
    engine = make_engine()
    decision = engine.select(
        make_request(),
        [
            ProviderState("provider-a", priority=100),
            ProviderState("provider-b", priority=1),
        ],
        [
            CredentialState("a", "provider-a"),
            CredentialState("b", "provider-b"),
        ],
    )

    assert decision.provider_model.provider_id == "provider-a"


def test_composite_score_selects_healthier_key_with_equal_priority() -> None:
    decision = make_engine().select(
        make_request(),
        [ProviderState("provider-a")],
        [
            CredentialState("nearly-empty", "provider-a", priority=10, quota_headroom=0.05),
            CredentialState("available", "provider-a", priority=10, quota_headroom=0.9),
        ],
    )

    assert decision.credential.credential_id == "available"


def test_excludes_cooldown_exhausted_and_failed_routes() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    decision = make_engine().select(
        make_request(),
        [
            ProviderState("provider-a", health=HealthState.DEGRADED),
            ProviderState("provider-b"),
        ],
        [
            CredentialState(
                "cooldown",
                "provider-a",
                cooldown_until=now + timedelta(minutes=1),
            ),
            CredentialState("quota", "provider-a", quota_headroom=0),
            CredentialState("fallback", "provider-b"),
        ],
        now=now,
    )

    assert decision.credential.credential_id == "fallback"


def test_excluded_key_allows_same_provider_rotation() -> None:
    decision = make_engine().select(
        make_request(),
        [ProviderState("provider-a")],
        [
            CredentialState("first", "provider-a", priority=10),
            CredentialState("second", "provider-a", priority=20),
        ],
        excluded_credential_ids=frozenset({"first"}),
    )

    assert decision.credential.credential_id == "second"


def test_excluded_provider_model_selects_alternate_route() -> None:
    decision = make_engine().select(
        make_request(),
        [ProviderState("provider-a"), ProviderState("provider-b")],
        [CredentialState("a", "provider-a"), CredentialState("b", "provider-b")],
        excluded_provider_model_ids=frozenset({"a-model-x"}),
    )

    assert decision.provider_model.id == "b-model-x"


def test_no_route_when_circuit_is_open() -> None:
    with pytest.raises(NoRouteAvailable):
        make_engine().select(
            make_request(),
            [ProviderState("provider-a", circuit_open=True)],
            [CredentialState("key", "provider-a")],
        )


def test_reasoning_request_selects_reasoning_capable_route() -> None:
    capabilities = frozenset({Capability.STREAMING, Capability.REASONING})
    registry = ModelRegistry(
        [CanonicalModel("model-x", frozenset(), capabilities)],
        [
            ProviderModel(
                id="reasoning-model-x",
                canonical_model_id="model-x",
                provider_id="provider-a",
                upstream_model_id="upstream-x",
                protocol=ClientProtocol.ANTHROPIC_MESSAGES,
                capabilities=capabilities,
            )
        ],
    )
    request = normalize_request(
        ClientProtocol.ANTHROPIC_MESSAGES,
        {
            "model": "model-x",
            "stream": True,
            "thinking": {"type": "enabled", "budget_tokens": 1024},
            "messages": [],
        },
    )

    decision = RoutingEngine(registry).select(
        request,
        [ProviderState("provider-a")],
        [CredentialState("key", "provider-a")],
    )

    assert decision.provider_model.id == "reasoning-model-x"


def test_route_policy_weights_override_default_scoring() -> None:
    model = CanonicalModel(
        id="model-x",
        aliases=frozenset(),
        capabilities=frozenset({Capability.STREAMING}),
    )
    routes = [
        ProviderModel(
            id="quota-route",
            canonical_model_id="model-x",
            provider_id="provider-a",
            upstream_model_id="a",
            protocol=ClientProtocol.ANTHROPIC_MESSAGES,
            capabilities=frozenset({Capability.STREAMING}),
            routing_policy={"quota_weight": 200, "latency_weight": 0},
        ),
        ProviderModel(
            id="latency-route",
            canonical_model_id="model-x",
            provider_id="provider-b",
            upstream_model_id="b",
            protocol=ClientProtocol.ANTHROPIC_MESSAGES,
            capabilities=frozenset({Capability.STREAMING}),
            routing_policy={"quota_weight": 0, "latency_weight": 100},
        ),
    ]
    engine = RoutingEngine(ModelRegistry([model], routes))

    decision = engine.select(
        make_request().model_copy(update={"requested_model": "model-x"}),
        [ProviderState("provider-a", latency_ms=5000), ProviderState("provider-b")],
        [
            CredentialState("a", "provider-a", quota_headroom=1),
            CredentialState("b", "provider-b", quota_headroom=0.1),
        ],
    )

    assert decision.provider_model.id == "quota-route"


def test_least_loaded_pool_prefers_available_concurrency_headroom() -> None:
    model = CanonicalModel(
        id="model-x",
        aliases=frozenset(),
        capabilities=frozenset({Capability.STREAMING}),
    )
    route = ProviderModel(
        id="pool-route",
        canonical_model_id="model-x",
        provider_id="provider-a",
        upstream_model_id="upstream-x",
        protocol=ClientProtocol.ANTHROPIC_MESSAGES,
        capabilities=frozenset({Capability.STREAMING}),
        pool_strategy="least_loaded",
        pool_members={"busy": {"priority": 1}, "available": {"priority": 1}},
    )
    decision = RoutingEngine(ModelRegistry([model], [route])).select(
        make_request().model_copy(update={"requested_model": "model-x"}),
        [ProviderState("provider-a")],
        [
            CredentialState("busy", "provider-a", concurrency_headroom=0.1),
            CredentialState("available", "provider-a", concurrency_headroom=0.9),
        ],
    )

    assert decision.credential.credential_id == "available"


def test_empty_pool_allowlist_fails_closed() -> None:
    capabilities = frozenset({Capability.STREAMING})
    engine = RoutingEngine(
        ModelRegistry(
            [CanonicalModel("model-x", frozenset({"latest-x"}), capabilities)],
            [
                ProviderModel(
                    id="pool-route",
                    canonical_model_id="model-x",
                    provider_id="provider-a",
                    upstream_model_id="upstream-x",
                    protocol=ClientProtocol.ANTHROPIC_MESSAGES,
                    capabilities=capabilities,
                    allowed_credential_ids=frozenset(),
                )
            ],
        )
    )

    with pytest.raises(NoRouteAvailable):
        engine.select(
            make_request(),
            [ProviderState("provider-a")],
            [CredentialState("credential", "provider-a")],
        )


def test_route_without_pool_allows_provider_credentials() -> None:
    decision = make_engine().select(
        make_request(),
        [ProviderState("provider-a")],
        [CredentialState("credential", "provider-a")],
    )

    assert decision.credential.credential_id == "credential"


def test_every_error_category_has_a_client_status() -> None:
    """A category with no status mapping raises a KeyError mid-response."""
    from gateway.api.errors import ERROR_STATUS
    from gateway.providers import ErrorCategory

    missing = [category for category in ErrorCategory if category not in ERROR_STATUS]
    assert not missing, f"categories with no HTTP status: {missing}"


def test_every_error_category_has_retry_semantics() -> None:
    from gateway.providers import ErrorCategory
    from gateway.providers.base import _RETRY_SEMANTICS

    missing = [category for category in ErrorCategory if category not in _RETRY_SEMANTICS]
    assert not missing, f"categories with no retry semantics: {missing}"


def test_no_eligible_route_is_distinct_from_a_missing_model() -> None:
    """"Nothing was eligible" and "this provider lacks the model" are not the same.

    Reporting exhausted capacity as model_unavailable sent the operator to look at
    mappings when the real cause was health, quota or cooldown.
    """
    from gateway.api.errors import ERROR_STATUS
    from gateway.providers import ErrorCategory
    from gateway.providers.base import build_provider_error

    assert ERROR_STATUS[ErrorCategory.MODEL_UNAVAILABLE] == 404
    assert ERROR_STATUS[ErrorCategory.NO_ELIGIBLE_ROUTE] == 503

    # Nothing was contacted, so there is nothing to retry within the request.
    error = build_provider_error(ErrorCategory.NO_ELIGIBLE_ROUTE, "none eligible")
    assert error.retryable is False
    assert error.credential_at_fault is False


def test_a_provider_404_is_visible_in_the_health_view() -> None:
    """model_unavailable previously suppressed even the health_checks row."""
    from gateway.observability import _passive_health

    assert _passive_health("model_unavailable") == "degraded"


def test_an_unhealthy_credential_earns_a_trial_once_its_cooldown_elapses() -> None:
    """An expired cooldown must make an unhealthy credential routable again.

    Health only degrades on its own and is restored by observing a success, so a
    credential that is never selected can never recover. In production this left
    AgentRouter with one routable credential out of twenty five, because ten
    rate-limited credentials held cooldowns that had already expired.
    """
    now = datetime(2026, 1, 1, tzinfo=UTC)
    decision = make_engine().select(
        make_request(),
        [ProviderState("provider-a")],
        [
            CredentialState(
                "rate-limited-but-cooled-off",
                "provider-a",
                health=HealthState.RATE_LIMITED,
                cooldown_until=now - timedelta(minutes=5),
            )
        ],
        now=now,
    )

    assert decision.credential.credential_id == "rate-limited-but-cooled-off"


def test_an_unhealthy_credential_stays_excluded_while_its_cooldown_runs() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(NoRouteAvailable):
        make_engine().select(
            make_request(),
            [ProviderState("provider-a")],
            [
                CredentialState(
                    "still-cooling",
                    "provider-a",
                    health=HealthState.RATE_LIMITED,
                    cooldown_until=now + timedelta(minutes=5),
                )
            ],
            now=now,
        )


def test_an_unhealthy_credential_without_a_cooldown_is_not_retried() -> None:
    """A key that never earned a cooldown gets no trial.

    A revoked or mistyped key fails without a retry-after, so it never receives a
    cooldown. Trialling those forever would spend an attempt on every request.
    """
    now = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(NoRouteAvailable):
        make_engine().select(
            make_request(),
            [ProviderState("provider-a")],
            [
                CredentialState(
                    "never-worked",
                    "provider-a",
                    health=HealthState.AUTH_FAILED,
                    cooldown_until=None,
                )
            ],
            now=now,
        )


def test_a_healthy_credential_is_preferred_over_one_on_trial() -> None:
    """Recovery must not cost throughput while a healthy credential exists."""
    now = datetime(2026, 1, 1, tzinfo=UTC)
    decision = make_engine().select(
        make_request(),
        [ProviderState("provider-a")],
        [
            CredentialState(
                "on-trial",
                "provider-a",
                health=HealthState.RATE_LIMITED,
                cooldown_until=now - timedelta(minutes=5),
            ),
            CredentialState("healthy", "provider-a"),
        ],
        now=now,
    )

    assert decision.credential.credential_id == "healthy"
