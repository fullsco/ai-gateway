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
