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


def _classifying_adapters() -> tuple:
    """One adapter per protocol, so every classification assertion covers both.

    AgentRouter answers the same credentials on /v1/messages and /v1/chat/completions,
    so a rule that holds on one protocol holds on the other. The two adapters used to
    carry separate copies of this logic and the copies drifted; asserting over both is
    what keeps them from drifting again.
    """
    from gateway.protocols import ClientProtocol
    from gateway.providers import ProviderConfig
    from gateway.providers.anthropic import AnthropicCompatibleAdapter
    from gateway.providers.openai import OpenAICompatibleAdapter

    def config(protocol: ClientProtocol) -> ProviderConfig:
        return ProviderConfig(
            id="p",
            name="P",
            base_url="https://upstream.example",
            protocol=protocol,
            capabilities=frozenset(),
        )

    return (
        AnthropicCompatibleAdapter(config(ClientProtocol.ANTHROPIC_MESSAGES)),
        OpenAICompatibleAdapter(config(ClientProtocol.OPENAI_CHAT_COMPLETIONS)),
    )


def test_a_403_that_says_out_of_quota_is_not_read_as_a_rejected_credential() -> None:
    """Quota and authentication need opposite responses, so they must not collapse.

    Some resellers answer 403 instead of 402 or 429 when a key is out of quota. The
    401/403 branch ran first, so those became upstream_authentication_error and the
    credential's health became auth_failed. That is wrong in a way that costs
    capacity: quota returns on its own, a rejected secret does not, and the operator
    is told to rotate a key that is fine. Two AgentRouter credentials sat parked
    that way while the provider was plainly reporting "user quota is not enough".
    """
    import httpx

    from gateway.providers import ErrorCategory

    for adapter in _classifying_adapters():
        out_of_quota = httpx.Response(
            403, json={"error": {"message": "user quota is not enough"}}
        )
        assert adapter.normalize_error(out_of_quota).category is ErrorCategory.QUOTA_EXHAUSTED

        # A 403 that is genuinely about the credential must still read that way, so
        # the fix cannot swallow real rejections. Both wordings below are ones the
        # credential probe actually recorded (deploy/act_on_credential_probe.py), and
        # both are properties of the key rather than of the caller: a sibling key may
        # well be on the allow list, or entitled to the model.
        for message in (
            "the caller's IP is not on the token's allow list",
            "the token may not access claude-opus-5",
            "invalid api key",
        ):
            rejected = httpx.Response(403, json={"error": {"message": message}})
            assert (
                adapter.normalize_error(rejected).category
                is ErrorCategory.UPSTREAM_AUTHENTICATION_ERROR
            ), message


def test_a_refusal_of_the_client_does_not_condemn_the_credential() -> None:
    """"unauthorized client detected" is a fingerprint check, not a key check.

    Probing eight AgentRouter credentials parked as auth_failed with a minimal header
    set returned this wording for all eight - the credential then serving every
    production request among them - while the mapping's real headers got 200 from four
    of them. Reading it as a rejected credential therefore walks the whole pool into
    auth_failed one key at a time, and points the operator at rotation when the fix is
    the request's headers.

    Both statuses are asserted because the two records of this disagree: the probe
    script logged 403, the relay findings logged 401. The wording is what identifies
    it, not the status.
    """
    import httpx

    from gateway.observability import _passive_health
    from gateway.providers import ErrorCategory
    from gateway.providers.base import RetryScope

    for adapter in _classifying_adapters():
        for status in (401, 403):
            gated = httpx.Response(
                status, json={"error": {"message": "unauthorized client detected"}}
            )
            error = adapter.normalize_error(gated)

            assert error.category is ErrorCategory.UPSTREAM_WAF_REJECTION, status
            # The invariant that matters: a valid credential must survive this.
            assert error.credential_at_fault is False, status
            assert _passive_health(error.category) != "auth_failed", status
            # Provider-scoped, so failover does not burn a sibling key on a refusal
            # that has nothing to do with which key was presented.
            assert error.retry_scope is RetryScope.PROVIDER, status
            assert error.retryable is True, status


def test_every_failure_state_earns_a_way_back() -> None:
    """A credential with no cooldown can never recover, so every state needs one.

    Health is restored by observing a success, and an unhealthy credential is never
    selected, so a cooldown is the only route back into service. Only rate limits
    used to set one. Everything else was stranded permanently, which is how fourteen
    AgentRouter credentials came to be parked with no cooldown at all, four of them
    working perfectly.
    """
    from gateway.observability import (
        _RECOVERY_COOLDOWN_SECONDS,
        PassiveHealthEvent,
        _passive_health,
        _recovery_cooldown,
    )
    from gateway.providers import ErrorCategory

    observed_at = datetime(2026, 1, 1, tzinfo=UTC)

    def event(retry_after: float | None = None) -> PassiveHealthEvent:
        return PassiveHealthEvent(
            provider_id="p", credential_id="c", provider_model_id="m",
            request_id="r", attempt_number=1, observed_at=observed_at,
            latency_ms=1.0, retry_after_seconds=retry_after,
        )

    # Every health state the gateway can write for a failure must be recoverable.
    reachable = {
        _passive_health(category.value)
        for category in ErrorCategory
        if _passive_health(category.value) not in (None, "healthy")
    }
    missing = reachable - set(_RECOVERY_COOLDOWN_SECONDS)
    assert not missing, f"these failure states can never be retried: {sorted(missing)}"

    for state in reachable:
        cooldown = _recovery_cooldown(state, event())
        assert cooldown is not None and cooldown > observed_at, state

    # A success must not park a working credential.
    assert _recovery_cooldown("healthy", event()) is None
    assert _recovery_cooldown(None, event()) is None

    # The provider's own retry-after beats our default, in both directions.
    assert _recovery_cooldown("rate_limited", event(retry_after=600)) == observed_at + timedelta(
        seconds=600
    )
    assert _recovery_cooldown("auth_failed", event(retry_after=5)) == observed_at + timedelta(
        seconds=5
    )


def test_a_flat_per_request_fee_is_priced_independently_of_tokens() -> None:
    """Some providers charge per request, and no per-token rate can describe that.

    Measured against TabiAi: 7,185 input tokens and 246,190 input tokens moved its
    billing counter by exactly the same amount, a 34x increase in size for no change
    in charge. Its published rate card agrees, listing claude-opus-5 at
    quota_type 1 with model_price 0.8 and model_ratio 0, and the counter delta of 80
    confirms the counter is in cents. Recording that as a per-million rate would
    misstate every request: too high for small ones, far too low for large ones.
    """
    from decimal import Decimal

    from gateway.observability import estimate_cost

    pricing = {"per_request": "0.80", "currency": "USD", "pricing_basis": "listed"}

    small, currency = estimate_cost((7185, 1, None, None), pricing)
    large, _ = estimate_cost((246190, 4000, None, None), pricing)
    assert currency == "USD"
    assert small == Decimal("0.80000000")
    assert large == small, "a flat fee must not vary with tokens"

    # A flat fee is owed even when the provider reported no usage, which is the
    # whole point: the request was charged for regardless, so recording it as free
    # would understate spend on exactly the requests that failed to report.
    unreported, _ = estimate_cost((None, None, None, None), pricing)
    assert unreported == Decimal("0.80000000")


def test_a_per_request_fee_cannot_be_mixed_with_token_rates() -> None:
    """The three pricing shapes describe incompatible billing models."""
    import pytest as _pytest

    from gateway.admin.control_plane import ProviderModelInput

    def build(pricing: dict) -> ProviderModelInput:
        return ProviderModelInput(
            provider_id="p",
            model_id="m",
            upstream_model_id="u",
            protocol="anthropic_messages",
            pricing=pricing,
        )

    accepted = build({"per_request": "0.30", "currency": "USD"})
    assert accepted.pricing["per_request"] == "0.30"
    assert accepted.pricing["pricing_basis"] == "listed"

    with _pytest.raises(ValueError):
        build({"per_request": "0.30", "input_per_million": "2", "output_per_million": "10",
               "currency": "USD"})
    with _pytest.raises(ValueError):
        build({"per_request": "0.30", "blended_per_million": "5", "currency": "USD"})
    # A flat fee is not a blended token rate, so it must not borrow that provenance.
    with _pytest.raises(ValueError):
        build({"per_request": "0.30", "currency": "USD", "pricing_basis": "measured_blended"})
