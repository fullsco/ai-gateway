"""Pure, advisory operating-envelope evaluation; never performs resource writes.

Call ``evaluate_operating_envelope(observed, OperatingEnvelopePolicy(...))`` with
one coherent observation window. Utilizations are fractions in [0, 1], ages/lag
are seconds, settlement_backlog and unknown_usage are nonnegative integer counts,
and dependency_healthy is a boolean. Omitted/None observations are unknown.

All policy defaults are PROVISIONAL, not production capacity qualifications:
two APIs * five connections + workflow five + maintenance five = twenty,
plus five reserved connections = the total budget of twenty-five. Worker pool
connections must include all workflow/accounting workers, not just one process.
Collectors, observation-window selection, and deployment decisions belong to the
caller. No environment, clocks, database, or deployment clients are consulted.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite


def _number(name: str, value: object, *, fraction: bool = False) -> None:
    if type(value) not in (int, float):
        raise ValueError(f"{name} must be a finite nonnegative number")
    try:
        finite = isfinite(value)
    except OverflowError:
        finite = False
    if not finite or value < 0 or (fraction and value > 1):
        bounds = "[0, 1]" if fraction else "[0, infinity)"
        raise ValueError(f"{name} must be finite and in {bounds}")


def _integer(name: str, value: object, *, minimum: int = 0) -> None:
    _number(name, value)
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")


@dataclass(frozen=True, kw_only=True)
class OperatingEnvelopePolicy:
    """Explicitly supplied policy; defaults are provisional starting values.

    Warning/scale-out thresholds are inclusive (>=). Low utilization is strictly
    below its threshold and only suggests a manual drain review, never scale-in.
    connection_budget is the TOTAL limit, including reserve_connections.
    """

    queue_age_seconds: float = 5.0
    oldest_settlement_seconds: float = 60.0
    settlement_backlog: int = 100
    db_pool_utilization: float = 0.9
    event_loop_lag_seconds: float = 0.1
    active_request_utilization: float = 0.8
    provider_utilization: float = 0.9
    config_age_seconds: float = 300.0
    low_active_request_utilization: float = 0.25
    current_replicas: int = 1
    max_replicas: int = 2
    connections_per_api: int = 5
    worker_connections: int = 5
    maintenance_connections: int = 5
    connection_budget: int = 25
    reserve_connections: int = 5

    def __post_init__(self) -> None:
        for name in (
            "queue_age_seconds", "oldest_settlement_seconds", "event_loop_lag_seconds",
            "config_age_seconds", "db_pool_utilization", "active_request_utilization",
            "provider_utilization", "low_active_request_utilization",
        ):
            value = getattr(self, name)
            _number(name, value, fraction=name.endswith("utilization"))
            if value == 0:
                raise ValueError(f"{name} threshold must be positive")
        for name in (
            "settlement_backlog", "current_replicas", "max_replicas",
            "connections_per_api", "connection_budget",
        ):
            _integer(name, getattr(self, name), minimum=1)
        for name in ("worker_connections", "maintenance_connections", "reserve_connections"):
            _integer(name, getattr(self, name))
        if self.reserve_connections >= self.connection_budget:
            raise ValueError("reserve_connections must be less than connection_budget")
        if self.low_active_request_utilization >= self.active_request_utilization:
            raise ValueError("low utilization threshold must be below scale-out threshold")
        # An already over-budget/over-max fleet is observable, not invalid policy.


_THRESHOLD_METRICS = (
    "queue_age_seconds", "oldest_settlement_seconds", "settlement_backlog",
    "db_pool_utilization", "event_loop_lag_seconds", "active_request_utilization",
    "provider_utilization", "config_age_seconds",
)
_METRICS = (*_THRESHOLD_METRICS, "unknown_usage", "dependency_healthy")


def evaluate_operating_envelope(
    observed: Mapping[str, object], policy: OperatingEnvelopePolicy,
) -> dict:
    """Return JSON-compatible statuses, warnings, recommendation and reason codes.

    Raises ValueError for invalid known observations/policy values. Extra metric
    keys are ignored to allow callers to pass a larger metrics snapshot. Missing
    evidence blocks scaling. A scale_out recommendation is only for ONE additional
    API replica; it is not authorization to deploy or bypass a drain check.
    """
    if not isinstance(observed, Mapping):
        raise ValueError("observed must be a metrics mapping")
    if not isinstance(policy, OperatingEnvelopePolicy):
        raise ValueError("policy must be an OperatingEnvelopePolicy")

    statuses = {}
    for name in _METRICS:
        value = observed.get(name)
        if value is None:
            status, reason = "unknown", "missing_observation"
        else:
            if name == "dependency_healthy":
                if type(value) is not bool:
                    raise ValueError("dependency_healthy must be a boolean")
                warning = not value
                reason = "dependency_unhealthy" if warning else "dependency_healthy"
            elif name == "unknown_usage":
                _integer(name, value)
                warning = value > 0
                reason = "unknown_usage_present" if warning else "no_unknown_usage"
            else:
                if name == "settlement_backlog":
                    _integer(name, value)
                else:
                    _number(name, value, fraction=name.endswith("utilization"))
                warning = value >= getattr(policy, name)
                reason = "threshold_reached" if warning else "below_threshold"
            status = "warning" if warning else "healthy"
        statuses[name] = {"status": status, "value": value, "reason": reason}

    def warned(name: str) -> bool:
        return statuses[name]["status"] == "warning"

    current_connections = (
        policy.current_replicas * policy.connections_per_api
        + policy.worker_connections + policy.maintenance_connections
    )
    projected_connections = current_connections + policy.connections_per_api
    usable_budget = policy.connection_budget - policy.reserve_connections
    budget_ok = projected_connections <= usable_budget
    replica_ok = policy.current_replicas < policy.max_replicas
    reasons = []
    if warned("dependency_healthy"):
        reasons.append("dependency_unhealthy")
    if warned("config_age_seconds"):
        reasons.append("config_stale")
    if warned("unknown_usage"):
        reasons.append("unknown_usage_present")
    reasons.extend(f"{name}_unknown" for name in _METRICS
                   if statuses[name]["status"] == "unknown")
    triggers = [name for name in (
        "active_request_utilization", "event_loop_lag_seconds", "queue_age_seconds",
    ) if warned(name)]

    if reasons:
        recommendation = "investigate"
    elif warned("provider_utilization"):
        recommendation, reasons = "no_scale", ["upstream_bound"]
    elif warned("db_pool_utilization"):
        recommendation, reasons = "no_scale", ["db_exhausted_review_connection_budget"]
    elif current_connections > usable_budget:
        recommendation, reasons = "no_scale", ["current_connection_budget_exceeded"]
    elif policy.current_replicas > policy.max_replicas:
        recommendation, reasons = "no_scale", ["current_replicas_exceed_max_drain_check_required"]
    elif warned("oldest_settlement_seconds") or warned("settlement_backlog"):
        recommendation, reasons = "investigate", ["settlement_processing_unhealthy"]
    elif triggers:
        reasons = [f"{name}_high" for name in triggers]
        if not budget_ok:
            reasons.append("projected_connection_budget_exceeded")
        if not replica_ok:
            reasons.append("max_replicas_reached")
        recommendation = "scale_out" if budget_ok and replica_ok else "no_scale"
    else:
        recommendation = "hold"
        reasons = [
            "low_utilization_drain_check_required"
            if observed["active_request_utilization"] < policy.low_active_request_utilization
            else "within_operating_envelope"
        ]

    return {
        "statuses": statuses,
        "warnings": [f"{name}:{entry['reason']}" for name, entry in statuses.items()
                     if entry["status"] != "healthy"],
        "recommendation": recommendation,
        "reasons": reasons,
        "connection_budget": {
            "current": current_connections,
            "projected": projected_connections,
            "total": policy.connection_budget,
            "reserve": policy.reserve_connections,
            "usable": usable_budget,
            "scale_out_fits": budget_ok,
        },
    }
