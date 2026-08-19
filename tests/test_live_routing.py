"""Behavioural proof for dynamic routing.

These tests assert the properties the snapshot architecture could not previously
provide: operational state (health, cooldown, quota, rate headroom, latency,
failure rate) changes routing *immediately*, with no configuration publish, while
explicit operator intent (priority, pools, policies) is still respected.
"""

from datetime import UTC, datetime, timedelta

import pytest

from gateway.configuration.snapshots import stranded_models
from gateway.models import CanonicalModel, ModelRegistry, ProviderModel
from gateway.protocols import Capability, ClientProtocol, NormalizedRequest
from gateway.providers import ErrorCategory, RetryScope, build_provider_error
from gateway.routing import CredentialState, ProviderState, RoutingEngine
from gateway.routing.engine import (
    HealthState,
    NoRouteAvailable,
    RoutingPolicy,
    RoutingTrace,
)
from gateway.routing.live_state import LiveOperationalState, QuotaConfidence, QuotaPolicy

CAPS = frozenset({Capability.STREAMING})


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def registry(*provider_models: ProviderModel) -> ModelRegistry:
    return ModelRegistry([CanonicalModel("m", frozenset({"m"}), CAPS)], list(provider_models))


def provider_model(
    pm_id: str, provider_id: str, *, priority: int = 100, weight: float = 1.0, **kw
) -> ProviderModel:
    return ProviderModel(
        pm_id,
        "m",
        provider_id,
        "upstream-m",
        ClientProtocol.ANTHROPIC_MESSAGES,
        CAPS,
        priority=priority,
        weight=weight,
        provider_name=provider_id,
        **kw,
    )


def request() -> NormalizedRequest:
    return NormalizedRequest(
        protocol=ClientProtocol.ANTHROPIC_MESSAGES,
        requested_model="m",
        payload={"model": "m"},
    )


def engine(*provider_models: ProviderModel, seed: int = 7) -> RoutingEngine:
    import random

    return RoutingEngine(registry(*provider_models), rng=random.Random(seed))


# --------------------------------------------------------------------- failures


def test_failed_credential_becomes_ineligible_without_a_publish() -> None:
    clock = FakeClock()
    live = LiveOperationalState(clock=clock)
    providers = [ProviderState("p1")]
    credentials = [
        CredentialState("c1", "p1", priority=10),
        CredentialState("c2", "p1", priority=20),
    ]
    eng = engine(provider_model("pm1", "p1"))

    # Primary is selected while everything is healthy.
    first = eng.select(request(), *live.overlay(providers, credentials)[:2])
    assert first.credential.credential_id == "c1"

    # An upstream rejection of c1 is folded into live state only.
    live.record_attempt(
        provider_id="p1",
        credential_id="c1",
        succeeded=False,
        error_category=ErrorCategory.UPSTREAM_AUTHENTICATION_ERROR.value,
    )

    # The very next selection avoids it. The snapshot was never republished.
    overlaid_providers, overlaid_credentials, _ = live.overlay(providers, credentials)
    second = eng.select(request(), overlaid_providers, overlaid_credentials)
    assert second.credential.credential_id == "c2"


def test_credential_recovers_without_a_publish_when_cooldown_expires() -> None:
    clock = FakeClock()
    live = LiveOperationalState(clock=clock)
    providers = [ProviderState("p1")]
    credentials = [CredentialState("c1", "p1")]
    eng = engine(provider_model("pm1", "p1"))

    live.record_attempt(
        provider_id="p1",
        credential_id="c1",
        succeeded=False,
        error_category=ErrorCategory.RATE_LIMIT.value,
        retry_after_seconds=30,
    )
    with pytest.raises(NoRouteAvailable):
        eng.select(request(), *live.overlay(providers, credentials)[:2])

    clock.advance(31)
    decision = eng.select(request(), *live.overlay(providers, credentials)[:2])
    assert decision.credential.credential_id == "c1"


def test_success_immediately_clears_a_local_penalty() -> None:
    clock = FakeClock()
    live = LiveOperationalState(clock=clock)
    providers = [ProviderState("p1")]
    credentials = [CredentialState("c1", "p1")]
    eng = engine(provider_model("pm1", "p1"))

    live.record_attempt(
        provider_id="p1",
        credential_id="c1",
        succeeded=False,
        error_category=ErrorCategory.UPSTREAM_AUTHENTICATION_ERROR.value,
    )
    with pytest.raises(NoRouteAvailable):
        eng.select(request(), *live.overlay(providers, credentials)[:2])

    live.record_attempt(provider_id="p1", credential_id="c1", succeeded=True)
    assert eng.select(request(), *live.overlay(providers, credentials)[:2])


def test_single_transport_blip_does_not_park_the_only_credential() -> None:
    """A provider-level failure must not remove the last way to reach a model."""
    clock = FakeClock()
    live = LiveOperationalState(clock=clock)
    providers = [ProviderState("p1")]
    credentials = [CredentialState("c1", "p1")]
    eng = engine(provider_model("pm1", "p1"))

    live.record_attempt(
        provider_id="p1",
        credential_id="c1",
        succeeded=False,
        error_category=ErrorCategory.PROVIDER_UNAVAILABLE.value,
        credential_at_fault=False,
    )
    assert eng.select(request(), *live.overlay(providers, credentials)[:2])


# ----------------------------------------------------------------- degradation


def test_degradation_shifts_traffic_before_total_provider_failure() -> None:
    """A provider does not have to be down to become less preferred."""
    clock = FakeClock()
    live = LiveOperationalState(clock=clock)
    providers = [ProviderState("p1"), ProviderState("p2")]
    credentials = [CredentialState("c1", "p1"), CredentialState("c2", "p2")]
    eng = engine(provider_model("pm1", "p1"), provider_model("pm2", "p2"))

    # p1 is still up but keeps producing provider-level failures.
    for _ in range(4):
        live.record_attempt(
            provider_id="p1",
            credential_id="c1",
            succeeded=False,
            error_category=ErrorCategory.PROVIDER_UNAVAILABLE.value,
            credential_at_fault=False,
        )
    live.record_attempt(provider_id="p2", credential_id="c2", succeeded=True)

    overlaid_providers, overlaid_credentials, _ = live.overlay(providers, credentials)
    p1 = next(p for p in overlaid_providers if p.provider_id == "p1")
    assert p1.health in {HealthState.HEALTHY, HealthState.DEGRADED}
    assert p1.failure_rate > 0

    # Both providers remain eligible, but traffic prefers the healthy one.
    picks = {
        eng.select(request(), overlaid_providers, overlaid_credentials).credential.credential_id
        for _ in range(20)
    }
    assert picks == {"c2"}


def test_latency_is_tracked_live_and_influences_score() -> None:
    clock = FakeClock()
    live = LiveOperationalState(clock=clock)
    providers = [ProviderState("p1"), ProviderState("p2")]
    credentials = [CredentialState("c1", "p1"), CredentialState("c2", "p2")]
    eng = engine(provider_model("pm1", "p1"), provider_model("pm2", "p2"))

    for _ in range(5):
        live.record_attempt(
            provider_id="p1", credential_id="c1", succeeded=True, latency_ms=5000
        )
        live.record_attempt(
            provider_id="p2", credential_id="c2", succeeded=True, latency_ms=50
        )
    overlaid = live.overlay(providers, credentials)
    picks = {eng.select(request(), *overlaid[:2]).credential.credential_id for _ in range(20)}
    assert picks == {"c2"}


# ------------------------------------------------------------ rate limit / rpm


def test_rpm_pressure_makes_a_credential_ineligible_then_recovers() -> None:
    clock = FakeClock()
    live = LiveOperationalState(clock=clock)
    providers = [ProviderState("p1")]
    credentials = [CredentialState("c1", "p1")]
    eng = engine(provider_model("pm1", "p1"))

    # Operator-configured limit of 5 requests/minute, observed in-process.
    live._credential("c1").requests_per_minute = 5  # noqa: SLF001 - test seam
    for _ in range(5):
        live.record_attempt(provider_id="p1", credential_id="c1", succeeded=True)

    _, overlaid_credentials, diagnostics = live.overlay(providers, credentials)
    assert overlaid_credentials[0].rpm_headroom == 0
    assert diagnostics["c1"]["rpm_headroom"] == 0
    with pytest.raises(NoRouteAvailable):
        eng.select(request(), providers, overlaid_credentials)

    # The window rolls forward; capacity returns with no publish.
    clock.advance(61)
    assert eng.select(request(), *live.overlay(providers, credentials)[:2])


def test_tpm_pressure_uses_observed_tokens() -> None:
    clock = FakeClock()
    live = LiveOperationalState(clock=clock)
    live._credential("c1").tokens_per_minute = 1000  # noqa: SLF001 - test seam
    live.record_attempt(
        provider_id="p1", credential_id="c1", succeeded=True, tokens=1000
    )
    _, credentials, diagnostics = live.overlay(
        [ProviderState("p1")], [CredentialState("c1", "p1")]
    )
    assert credentials[0].tpm_headroom == 0
    assert diagnostics["c1"]["tpm_headroom"] == 0


# ------------------------------------------------------------------ quota work


def test_known_quota_below_hard_threshold_stops_routing_and_recovers() -> None:
    clock = FakeClock()
    live = LiveOperationalState(
        clock=clock, quota_policy=QuotaPolicy(soft_threshold=0.2, hard_threshold=0.05)
    )
    providers = [ProviderState("p1")]
    credentials = [CredentialState("c1", "p1")]
    eng = engine(provider_model("pm1", "p1"))

    runtime = live._credential("c1")  # noqa: SLF001 - test seam
    runtime.quota_limit = 100.0
    runtime.quota_used = 99.0
    runtime.quota_confidence = QuotaConfidence.KNOWN

    _, overlaid, diagnostics = live.overlay(providers, credentials)
    assert diagnostics["c1"]["quota_confidence"] == QuotaConfidence.KNOWN
    assert overlaid[0].quota_headroom == 0
    with pytest.raises(NoRouteAvailable):
        eng.select(request(), providers, overlaid)

    # Quota is a routing state, not a destructive credential state: when the real
    # budget recovers the credential becomes eligible again by itself.
    runtime.quota_used = 10.0
    assert eng.select(request(), *live.overlay(providers, credentials)[:2])


def test_known_quota_below_soft_threshold_deprioritises_but_stays_eligible() -> None:
    clock = FakeClock()
    live = LiveOperationalState(
        clock=clock, quota_policy=QuotaPolicy(soft_threshold=0.5, hard_threshold=0.01)
    )
    providers = [ProviderState("p1")]
    credentials = [
        CredentialState("c1", "p1"),
        CredentialState("c2", "p1"),
    ]
    eng = engine(provider_model("pm1", "p1"))

    low = live._credential("c1")  # noqa: SLF001 - test seam
    low.quota_limit, low.quota_used = 100.0, 90.0  # 10% headroom
    low.quota_confidence = QuotaConfidence.KNOWN
    full = live._credential("c2")  # noqa: SLF001 - test seam
    full.quota_limit, full.quota_used = 100.0, 0.0
    full.quota_confidence = QuotaConfidence.KNOWN

    overlaid_providers, overlaid_credentials, _ = live.overlay(providers, credentials)
    # Still eligible ...
    assert all(c.quota_headroom > 0 for c in overlaid_credentials)
    # ... but traffic drains to the credential with budget.
    picks = {
        eng.select(request(), overlaid_providers, overlaid_credentials).credential.credential_id
        for _ in range(30)
    }
    assert picks == {"c2"}


def test_unknown_quota_never_makes_a_credential_ineligible() -> None:
    """We must not invent budget information we do not have."""
    live = LiveOperationalState(clock=FakeClock())
    live.record_attempt(provider_id="p1", credential_id="c1", succeeded=True)
    _, credentials, diagnostics = live.overlay(
        [ProviderState("p1")], [CredentialState("c1", "p1")]
    )
    assert diagnostics["c1"]["quota_confidence"] == QuotaConfidence.UNKNOWN
    assert credentials[0].quota_headroom == 1
    assert engine(provider_model("pm1", "p1")).select(
        request(), [ProviderState("p1")], credentials
    )


# ------------------------------------------------------------------ balancing


def test_traffic_is_distributed_across_many_equal_credentials() -> None:
    """A pool of keys must be shared, not pinned to one until it fails."""
    live = LiveOperationalState(clock=FakeClock())
    providers = [ProviderState("p1")]
    credentials = [CredentialState(f"c{i}", "p1") for i in range(10)]
    eng = engine(provider_model("pm1", "p1"))

    overlaid = live.overlay(providers, credentials)
    picks = [eng.select(request(), *overlaid[:2]).credential.credential_id for _ in range(300)]
    assert len(set(picks)) == 10, set(picks)


def test_explicit_priority_is_never_load_balanced_away() -> None:
    """Distribution happens inside a priority tier, never across tiers."""
    live = LiveOperationalState(clock=FakeClock())
    providers = [ProviderState("p1")]
    credentials = [
        CredentialState("primary", "p1", priority=10),
        CredentialState("secondary", "p1", priority=50),
    ]
    eng = engine(provider_model("pm1", "p1"))
    overlaid = live.overlay(providers, credentials)
    picks = {eng.select(request(), *overlaid[:2]).credential.credential_id for _ in range(50)}
    assert picks == {"primary"}


def test_route_priority_beats_credential_distribution_across_providers() -> None:
    live = LiveOperationalState(clock=FakeClock())
    providers = [ProviderState("p1"), ProviderState("p2")]
    credentials = [CredentialState("c1", "p1"), CredentialState("c2", "p2")]
    eng = engine(
        provider_model("pm1", "p1", priority=10),
        provider_model("pm2", "p2", priority=90),
    )
    overlaid = live.overlay(providers, credentials)
    picks = {eng.select(request(), *overlaid[:2]).credential.credential_id for _ in range(50)}
    assert picks == {"c1"}


def test_provider_fallback_used_once_the_pool_is_exhausted() -> None:
    live = LiveOperationalState(clock=FakeClock())
    providers = [ProviderState("p1"), ProviderState("p2")]
    credentials = [
        CredentialState("c1", "p1", priority=10),
        CredentialState("c2", "p1", priority=20),
        CredentialState("fallback", "p2", priority=10),
    ]
    eng = engine(
        provider_model("pm1", "p1", priority=10),
        provider_model("pm2", "p2", priority=90),
    )
    overlaid_providers, overlaid_credentials, _ = live.overlay(providers, credentials)
    decision = eng.select(
        request(),
        overlaid_providers,
        overlaid_credentials,
        excluded_credential_ids=frozenset({"c1", "c2"}),
    )
    assert decision.credential.credential_id == "fallback"
    assert decision.provider_model.provider_id == "p2"


# --------------------------------------------------------------- explainability


def test_trace_explains_considered_excluded_selected_and_fallback() -> None:
    clock = FakeClock()
    live = LiveOperationalState(clock=clock)
    providers = [ProviderState("p1"), ProviderState("p2", enabled=False)]
    credentials = [
        CredentialState("healthy", "p1", priority=20),
        CredentialState("cooling", "p1", priority=10),
        CredentialState("other", "p2"),
    ]
    eng = engine(provider_model("pm1", "p1"), provider_model("pm2", "p2"))

    live.record_attempt(
        provider_id="p1",
        credential_id="cooling",
        succeeded=False,
        error_category=ErrorCategory.RATE_LIMIT.value,
        retry_after_seconds=60,
    )
    overlaid_providers, overlaid_credentials, diagnostics = live.overlay(providers, credentials)

    trace = RoutingTrace(attempt_number=2)
    trace.is_fallback = True
    trace.fallback_reason = ErrorCategory.RATE_LIMIT.value
    decision = eng.select(
        request(),
        overlaid_providers,
        overlaid_credentials,
        trace=trace,
        diagnostics=diagnostics,
    )
    payload = trace.as_dict()

    assert decision.credential.credential_id == "healthy"
    assert payload["selected"]["credential_id"] == "healthy"
    assert payload["is_fallback"] is True
    assert payload["fallback_reason"] == ErrorCategory.RATE_LIMIT.value

    reasons = {
        (item["credential_id"], item["reason"])
        for item in payload["considered"]
        if not item["eligible"]
    }
    # Health is evaluated before cooldown, so a rate-limited credential is
    # reported by its health state; the cooldown itself is visible in "live".
    assert ("cooling", "credential_health_rate_limited") in reasons
    assert any(reason == "provider_disabled" for _, reason in reasons)

    # Live signals that drove the decision are attached for observability.
    cooling = next(i for i in payload["considered"] if i["credential_id"] == "cooling")
    assert cooling["live"]["last_error_category"] == ErrorCategory.RATE_LIMIT.value
    assert cooling["live"]["cooldown_until"] is not None


def test_trace_records_quota_confidence() -> None:
    live = LiveOperationalState(clock=FakeClock())
    runtime = live._credential("c1")  # noqa: SLF001 - test seam
    runtime.quota_limit, runtime.quota_used = 100.0, 50.0
    runtime.quota_confidence = QuotaConfidence.KNOWN
    overlaid_providers, overlaid_credentials, diagnostics = live.overlay(
        [ProviderState("p1")], [CredentialState("c1", "p1")]
    )
    trace = RoutingTrace()
    engine(provider_model("pm1", "p1")).select(
        request(), overlaid_providers, overlaid_credentials, trace=trace, diagnostics=diagnostics
    )
    entry = trace.as_dict()["considered"][0]
    assert entry["live"]["quota_confidence"] == QuotaConfidence.KNOWN
    assert entry["live"]["quota_headroom"] == 0.5


def test_exclusion_reasons_cover_pool_and_policy_rules() -> None:
    live = LiveOperationalState(clock=FakeClock())
    providers = [ProviderState("p1")]
    credentials = [CredentialState("in-pool", "p1"), CredentialState("out-of-pool", "p1")]
    eng = engine(
        provider_model("pm1", "p1", allowed_credential_ids=frozenset({"in-pool"}))
    )
    trace = RoutingTrace()
    decision = eng.select(request(), *live.overlay(providers, credentials)[:2], trace=trace)
    assert decision.credential.credential_id == "in-pool"
    excluded = {i["credential_id"]: i["reason"] for i in trace.excluded}
    assert excluded["out-of-pool"] == "credential_not_in_route_pool"


# ---------------------------------------------------------------- retry scopes


@pytest.mark.parametrize(
    ("category", "retryable", "scope", "credential_at_fault"),
    [
        (ErrorCategory.AUTHENTICATION_ERROR, False, RetryScope.NONE, False),
        (ErrorCategory.UPSTREAM_AUTHENTICATION_ERROR, True, RetryScope.CREDENTIAL, True),
        (ErrorCategory.RATE_LIMIT, True, RetryScope.CREDENTIAL, True),
        (ErrorCategory.QUOTA_EXHAUSTED, True, RetryScope.CREDENTIAL, True),
        (ErrorCategory.UPSTREAM_WAF_REJECTION, True, RetryScope.PROVIDER, False),
        (ErrorCategory.PROVIDER_UNAVAILABLE, True, RetryScope.PROVIDER, False),
        (ErrorCategory.TIMEOUT, True, RetryScope.PROVIDER, False),
        (ErrorCategory.MODEL_UNAVAILABLE, True, RetryScope.PROVIDER, False),
        (ErrorCategory.INVALID_REQUEST, False, RetryScope.NONE, False),
        (ErrorCategory.INTERNAL_ERROR, False, RetryScope.NONE, False),
    ],
)
def test_retry_semantics_follow_failure_meaning(
    category: ErrorCategory, retryable: bool, scope: RetryScope, credential_at_fault: bool
) -> None:
    error = build_provider_error(category, "boom")
    assert error.retryable is retryable
    assert error.retry_scope is scope
    assert error.credential_at_fault is credential_at_fault


def test_client_auth_failure_never_penalises_a_credential() -> None:
    """A bad *gateway* key must not mark an upstream credential unhealthy."""
    live = LiveOperationalState(clock=FakeClock())
    error = build_provider_error(ErrorCategory.AUTHENTICATION_ERROR, "bad gateway key")
    live.record_attempt(
        provider_id="p1",
        credential_id="c1",
        succeeded=False,
        error_category=error.category.value,
        credential_at_fault=error.credential_at_fault,
    )
    _, credentials, _ = live.overlay([ProviderState("p1")], [CredentialState("c1", "p1")])
    assert credentials[0].health is HealthState.HEALTHY
    assert credentials[0].cooldown_until is None


# ------------------------------------------------------------- snapshot guards


def _snapshot(**overrides):
    payload = {
        "models": [{"id": "m", "enabled": True}],
        "providers": [{"id": "p1", "enabled": True}],
        "credentials": [{"id": "c1", "provider_id": "p1", "enabled": True}],
        "provider_models": [
            {
                "id": "pm1",
                "provider_id": "p1",
                "canonical_model_id": "m",
                "enabled": True,
                "allowed_credential_ids": None,
            }
        ],
    }
    payload.update(overrides)
    return payload


def test_publish_guard_accepts_a_usable_snapshot() -> None:
    assert stranded_models(_snapshot()) == []


def test_publish_guard_detects_model_without_any_mapping() -> None:
    assert stranded_models(_snapshot(provider_models=[])) == ["m"]


def test_publish_guard_detects_disabled_provider() -> None:
    assert stranded_models(_snapshot(providers=[{"id": "p1", "enabled": False}])) == ["m"]


def test_publish_guard_detects_empty_credential_pool() -> None:
    payload = _snapshot()
    payload["provider_models"][0]["allowed_credential_ids"] = []
    assert stranded_models(payload) == ["m"]


def test_publish_guard_detects_no_enabled_credential() -> None:
    payload = _snapshot(
        credentials=[{"id": "c1", "provider_id": "p1", "enabled": False}]
    )
    assert stranded_models(payload) == ["m"]


def test_publish_guard_ignores_transient_health() -> None:
    """A rate-limited credential must not make a configuration unpublishable."""
    payload = _snapshot(
        credentials=[
            {"id": "c1", "provider_id": "p1", "enabled": True, "health": "rate_limited"}
        ]
    )
    assert stranded_models(payload) == []


def test_publish_guard_ignores_disabled_models() -> None:
    payload = _snapshot(models=[{"id": "m", "enabled": False}], provider_models=[])
    assert stranded_models(payload) == []


# ------------------------------------------------------------------- overlay io


def test_overlay_prefers_database_health_over_snapshot_health() -> None:
    """Operator or worker changes take effect without republishing."""
    live = LiveOperationalState(clock=FakeClock())
    live._credential("c1").db_health = HealthState.AUTH_FAILED  # noqa: SLF001
    _, credentials, _ = live.overlay([ProviderState("p1")], [CredentialState("c1", "p1")])
    assert credentials[0].health is HealthState.AUTH_FAILED


def test_overlay_honours_database_cooldown() -> None:
    live = LiveOperationalState(clock=FakeClock())
    future = datetime.now(UTC) + timedelta(minutes=5)
    live._credential("c1").db_cooldown_until = future  # noqa: SLF001
    _, credentials, _ = live.overlay([ProviderState("p1")], [CredentialState("c1", "p1")])
    assert credentials[0].cooldown_until == future
    with pytest.raises(NoRouteAvailable):
        engine(provider_model("pm1", "p1")).select(
            request(), [ProviderState("p1")], credentials
        )


def test_overlay_disables_credential_disabled_in_database() -> None:
    live = LiveOperationalState(clock=FakeClock())
    live._credential("c1").enabled = False  # noqa: SLF001
    _, credentials, _ = live.overlay([ProviderState("p1")], [CredentialState("c1", "p1")])
    assert credentials[0].enabled is False


def test_weighted_strategy_spreads_by_weight() -> None:
    live = LiveOperationalState(clock=FakeClock())
    providers = [ProviderState("p1")]
    credentials = [CredentialState("c1", "p1"), CredentialState("c2", "p1")]
    eng = engine(
        provider_model(
            "pm1",
            "p1",
            pool_strategy="weighted",
            pool_members={"c1": {"weight": 9, "priority": 100},
                          "c2": {"weight": 1, "priority": 100}},
        )
    )
    overlaid = live.overlay(providers, credentials)
    picks = [eng.select(request(), *overlaid[:2]).credential.credential_id for _ in range(400)]
    assert picks.count("c1") > picks.count("c2") * 3


def test_least_loaded_strategy_is_deterministic() -> None:
    live = LiveOperationalState(clock=FakeClock())
    providers = [ProviderState("p1")]
    credentials = [
        CredentialState("busy", "p1", concurrency_headroom=0.1),
        CredentialState("free", "p1", concurrency_headroom=1.0),
    ]
    eng = engine(provider_model("pm1", "p1", pool_strategy="least_loaded"))
    overlaid = live.overlay(providers, credentials)
    picks = {eng.select(request(), *overlaid[:2]).credential.credential_id for _ in range(30)}
    assert picks == {"free"}


def test_policy_can_disable_distribution() -> None:
    live = LiveOperationalState(clock=FakeClock())
    providers = [ProviderState("p1")]
    credentials = [CredentialState(f"c{i}", "p1") for i in range(5)]
    eng = RoutingEngine(
        registry(provider_model("pm1", "p1")),
        policy=RoutingPolicy(distribute_within_tier=False),
    )
    overlaid = live.overlay(providers, credentials)
    picks = {eng.select(request(), *overlaid[:2]).credential.credential_id for _ in range(20)}
    assert len(picks) == 1


def test_persisted_unhealthy_credential_gets_a_half_open_retry() -> None:
    """A credential marked unavailable must not be excluded forever."""
    clock = FakeClock()
    live = LiveOperationalState(clock=clock)
    providers = [ProviderState("p1")]
    credentials = [CredentialState("c1", "p1")]
    eng = engine(provider_model("pm1", "p1"))

    runtime = live._credential("c1")  # noqa: SLF001 - test seam
    runtime.db_health = HealthState.UNAVAILABLE
    runtime.db_unhealthy_since = clock.now

    # Immediately after the failure it stays out of rotation.
    with pytest.raises(NoRouteAvailable):
        eng.select(request(), *live.overlay(providers, credentials)[:2])

    # After the grace period it is retried as DEGRADED, without any publish.
    clock.advance(61)
    _, overlaid, _ = live.overlay(providers, credentials)
    assert overlaid[0].health is HealthState.DEGRADED
    assert eng.select(request(), providers, overlaid)


def test_half_open_retry_still_loses_to_a_healthy_credential() -> None:
    clock = FakeClock()
    live = LiveOperationalState(clock=clock)
    providers = [ProviderState("p1")]
    credentials = [CredentialState("sick", "p1"), CredentialState("well", "p1")]
    eng = engine(provider_model("pm1", "p1"))
    runtime = live._credential("sick")  # noqa: SLF001 - test seam
    runtime.db_health = HealthState.UNAVAILABLE
    runtime.db_unhealthy_since = clock.now
    clock.advance(61)
    overlaid = live.overlay(providers, credentials)
    picks = {eng.select(request(), *overlaid[:2]).credential.credential_id for _ in range(40)}
    assert picks == {"well"}


@pytest.mark.parametrize(
    "state",
    [
        HealthState.RATE_LIMITED,
        HealthState.AUTH_FAILED,
        HealthState.QUOTA_EXHAUSTED,
        HealthState.UNAVAILABLE,
        HealthState.COOLDOWN,
    ],
)
def test_every_unroutable_state_is_eligible_for_half_open_recovery(state) -> None:
    """Any state the engine refuses must be retryable, or it deadlocks.

    Regression test: rate_limited was excluded by the engine but missing from the
    recovery set, so a rate-limited credential could never be tried again and a
    single-credential model stayed unavailable indefinitely.
    """
    clock = FakeClock()
    live = LiveOperationalState(clock=clock)
    providers = [ProviderState("p1")]
    credentials = [CredentialState("c1", "p1")]
    eng = engine(provider_model("pm1", "p1"))

    runtime = live._credential("c1")  # noqa: SLF001 - test seam
    runtime.db_health = state
    runtime.db_unhealthy_since = clock.now

    with pytest.raises(NoRouteAvailable):
        eng.select(request(), *live.overlay(providers, credentials)[:2])

    clock.advance(61)
    _, overlaid, _ = live.overlay(providers, credentials)
    assert overlaid[0].health is HealthState.DEGRADED
    assert eng.select(request(), providers, overlaid)
