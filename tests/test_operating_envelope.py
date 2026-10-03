import copy
import json
from dataclasses import replace

import pytest

from gateway.operating_envelope import OperatingEnvelopePolicy, evaluate_operating_envelope


@pytest.fixture
def observed():
    return {
        "queue_age_seconds": 0,
        "oldest_settlement_seconds": 0,
        "settlement_backlog": 0,
        "unknown_usage": 0,
        "db_pool_utilization": 0.2,
        "event_loop_lag_seconds": 0.01,
        "active_request_utilization": 0.5,
        "provider_utilization": 0.2,
        "config_age_seconds": 10,
        "dependency_healthy": True,
    }


def evaluate(observed, **policy):
    return evaluate_operating_envelope(observed, OperatingEnvelopePolicy(**policy))


def test_healthy_is_deterministic_serializable_and_does_not_mutate(observed):
    original = copy.deepcopy(observed)
    result = evaluate(observed)
    assert result == evaluate(dict(reversed(list(observed.items()))))
    assert observed == original
    assert json.loads(json.dumps(result, allow_nan=False)) == result
    assert result["recommendation"] == "hold"
    assert result["reasons"] == ["within_operating_envelope"]
    assert result["warnings"] == []
    assert all(item["status"] == "healthy" for item in result["statuses"].values())
    result["statuses"].clear()
    assert evaluate(observed)["statuses"]


@pytest.mark.parametrize("metric", [
    "active_request_utilization", "event_loop_lag_seconds", "queue_age_seconds",
])
def test_scale_trigger_is_inclusive_and_respects_explicit_threshold(observed, metric):
    threshold = getattr(OperatingEnvelopePolicy(), metric)
    observed[metric] = threshold - 0.0001
    assert evaluate(observed)["recommendation"] == "hold"
    observed[metric] = threshold
    result = evaluate(observed)
    assert result["recommendation"] == "scale_out"
    assert result["reasons"] == [f"{metric}_high"]
    assert result["statuses"][metric]["status"] == "warning"
    assert evaluate(observed, **{metric: threshold + 0.01})["recommendation"] == "hold"


@pytest.mark.parametrize(("metric", "value", "recommendation", "reason"), [
    ("dependency_healthy", False, "investigate", "dependency_unhealthy"),
    ("unknown_usage", 1, "investigate", "unknown_usage_present"),
    ("config_age_seconds", 300, "investigate", "config_stale"),
    ("provider_utilization", 0.9, "no_scale", "upstream_bound"),
    ("db_pool_utilization", 0.9, "no_scale", "db_exhausted_review_connection_budget"),
    ("oldest_settlement_seconds", 60, "investigate", "settlement_processing_unhealthy"),
    ("settlement_backlog", 100, "investigate", "settlement_processing_unhealthy"),
])
def test_blockers_override_all_scale_triggers(observed, metric, value, recommendation, reason):
    observed.update(active_request_utilization=1, event_loop_lag_seconds=1, queue_age_seconds=10)
    observed[metric] = value
    result = evaluate(observed)
    assert result["recommendation"] == recommendation
    assert result["reasons"] == [reason]
    assert result["statuses"][metric]["status"] == "warning"
    assert any(warning.startswith(f"{metric}:") for warning in result["warnings"])


def test_investigation_precedes_upstream_and_db_with_all_reasons_preserved(observed):
    observed.update(dependency_healthy=False, config_age_seconds=300, unknown_usage=1,
                    provider_utilization=1, db_pool_utilization=1, queue_age_seconds=10)
    result = evaluate(observed)
    assert result["recommendation"] == "investigate"
    assert result["reasons"] == ["dependency_unhealthy", "config_stale", "unknown_usage_present"]
    assert len(result["warnings"]) == 6
    observed.update(dependency_healthy=True, config_age_seconds=0, unknown_usage=0)
    assert evaluate(observed)["reasons"] == ["upstream_bound"]


def test_provisional_budget_includes_reserve_and_exact_boundary(observed):
    observed["queue_age_seconds"] = 10
    result = evaluate(observed)
    assert result["recommendation"] == "scale_out"
    assert result["connection_budget"] == {
        "current": 15, "projected": 20, "total": 25, "reserve": 5,
        "usable": 20, "scale_out_fits": True,
    }
    for changes in (
        {"connection_budget": 24}, {"reserve_connections": 6},
        {"worker_connections": 6}, {"maintenance_connections": 6},
        {"connections_per_api": 6},
    ):
        blocked = evaluate(observed, **changes)
        assert blocked["recommendation"] == "no_scale"
        assert "projected_connection_budget_exceeded" in blocked["reasons"]
    assert evaluate(observed, connection_budget=20, reserve_connections=0)[
        "recommendation"
    ] == "scale_out"


def test_max_replica_gate_is_independent_of_budget(observed):
    observed["active_request_utilization"] = 0.9
    result = evaluate(observed, current_replicas=2, connection_budget=100)
    assert result["recommendation"] == "no_scale"
    assert result["reasons"] == ["active_request_utilization_high", "max_replicas_reached"]
    assert evaluate(observed, current_replicas=2, max_replicas=3, connection_budget=100)[
        "recommendation"
    ] == "scale_out"


def test_existing_over_budget_or_over_max_fleet_is_not_healthy(observed):
    assert evaluate(observed, connection_budget=19)["reasons"] == [
        "current_connection_budget_exceeded",
    ]
    assert evaluate(observed, current_replicas=3, connection_budget=100)["reasons"] == [
        "current_replicas_exceed_max_drain_check_required",
    ]


def test_low_utilization_needs_drain_check_and_does_not_override_queue(observed):
    observed["active_request_utilization"] = 0
    result = evaluate(observed)
    assert result["recommendation"] == "hold"
    assert result["reasons"] == ["low_utilization_drain_check_required"]
    observed["queue_age_seconds"] = 5
    assert evaluate(observed)["recommendation"] == "scale_out"


@pytest.mark.parametrize("missing", [
    "queue_age_seconds", "oldest_settlement_seconds", "settlement_backlog", "unknown_usage",
    "db_pool_utilization", "event_loop_lag_seconds", "active_request_utilization",
    "provider_utilization", "config_age_seconds", "dependency_healthy",
])
@pytest.mark.parametrize("use_none", [False, True])
def test_every_missing_observation_is_unknown_and_blocks_scaling(observed, missing, use_none):
    observed["queue_age_seconds"] = 10
    if use_none:
        observed[missing] = None
    else:
        del observed[missing]
    result = evaluate(observed)
    assert result["recommendation"] == "investigate"
    assert f"{missing}_unknown" in result["reasons"]
    assert result["statuses"][missing] == {
        "status": "unknown", "value": None, "reason": "missing_observation",
    }


def test_empty_snapshot_is_unknown_not_healthy():
    result = evaluate({})
    assert result["recommendation"] == "investigate"
    assert len(result["warnings"]) == len(result["statuses"]) == 10
    assert all(item["status"] == "unknown" for item in result["statuses"].values())


@pytest.mark.parametrize("metric", [
    "queue_age_seconds", "oldest_settlement_seconds", "settlement_backlog", "unknown_usage",
    "db_pool_utilization", "event_loop_lag_seconds", "active_request_utilization",
    "provider_utilization", "config_age_seconds",
])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), -1, True, "1", []])
def test_invalid_metric_rejected_even_when_investigation_gate_fails(observed, metric, value):
    observed.update(dependency_healthy=False)
    observed[metric] = value
    with pytest.raises(ValueError, match=metric):
        evaluate(observed)


@pytest.mark.parametrize(("metric", "value"), [
    ("db_pool_utilization", 1.01), ("active_request_utilization", 1.01),
    ("provider_utilization", 1.01), ("dependency_healthy", 1),
    ("dependency_healthy", "true"), ("unknown_usage", 0.5), ("settlement_backlog", 1.5),
    ("queue_age_seconds", 10 ** 1000),
])
def test_metric_domains_are_strict(observed, metric, value):
    observed[metric] = value
    with pytest.raises(ValueError, match=metric):
        evaluate(observed)


@pytest.mark.parametrize("changes", [
    {"queue_age_seconds": float("nan")}, {"config_age_seconds": float("inf")},
    {"db_pool_utilization": 1.1}, {"active_request_utilization": 0},
    {"event_loop_lag_seconds": -1}, {"provider_utilization": True},
    {"oldest_settlement_seconds": "60"}, {"settlement_backlog": 1.5},
    {"current_replicas": 0}, {"max_replicas": 0}, {"connections_per_api": 0},
    {"connection_budget": 0}, {"worker_connections": -1}, {"maintenance_connections": True},
    {"reserve_connections": 25}, {"reserve_connections": float("inf")},
    {"low_active_request_utilization": 0.8}, {"max_replicas": None},
])
def test_invalid_policy_rejected(changes):
    with pytest.raises(ValueError):
        replace(OperatingEnvelopePolicy(), **changes)


def test_evaluator_requires_explicit_policy_and_mapping(observed):
    with pytest.raises(TypeError):
        evaluate_operating_envelope(observed)
    with pytest.raises(ValueError, match="policy"):
        evaluate_operating_envelope(observed, {})
    with pytest.raises(ValueError, match="mapping"):
        evaluate(None)
