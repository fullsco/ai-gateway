"""Evaluate operational alert conditions and close them when they clear.

Request-time events can only describe a single attempt. The conditions an operator
actually cares about are aggregates over a window - a failure rate, a run of
authentication failures, a provider with nothing routable left - so they are
evaluated here on a schedule against recorded state.

Every condition is expressed as a query that returns the scopes currently in
breach together with the values that put them there. Anything previously in breach
and now absent is resolved, which is what lets the operator distinguish a live
problem from one that has already recovered.
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

from gateway.alerts import condition_matches, raise_alert, resolve_alerts
from gateway.logging import log_event

logger = logging.getLogger("gateway.alerts.monitor")

# Each condition returns rows of (scope_id, scope_label, observed...). window and
# threshold values come from the rule, so the same condition serves several rules
# at different sensitivities.
_CONDITIONS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "credential_quota_low": (
        "credential",
        """
        select c.id::text as scope_id, p.name || ' / ' || c.name as label,
               c.quota_used as quota_used, c.quota_limit as quota_limit,
               round((1 - c.quota_used / nullif(c.quota_limit,0))::numeric, 4) as headroom
        from public.provider_credentials c
        join public.providers p on p.id = c.provider_id
        where c.enabled and c.quota_limit is not null and c.quota_limit <> 0
          and c.quota_used is not null
          and (1 - c.quota_used / nullif(c.quota_limit,0)) <= $1::numeric
        """,
        ("threshold",),
    ),
    "credential_balance_low": (
        "credential",
        """
        select c.id::text as scope_id, p.name || ' / ' || c.name as label,
               c.balance_amount as balance, c.balance_currency as currency,
               c.balance_observed_at as observed_at
        from public.provider_credentials c
        join public.providers p on p.id = c.provider_id
        where c.enabled and c.balance_amount is not null
          and c.balance_amount <= $1::numeric
        """,
        ("threshold",),
    ),
    "credential_auth_failures": (
        "credential",
        """
        select a.credential_id::text as scope_id,
               p.name || ' / ' || c.name as label,
               count(*) as failures
        from public.request_attempts a
        join public.provider_credentials c on c.id = a.credential_id
        join public.providers p on p.id = a.provider_id
        where a.started_at >= now() - make_interval(mins => $1::int)
          and a.error_category = 'upstream_authentication_error'
        group by 1, 2 having count(*) >= $2::numeric
        """,
        ("window", "threshold"),
    ),
    "provider_failure_rate": (
        "provider",
        """
        select a.provider_id::text as scope_id, p.name as label,
               count(*) as attempts,
               count(*) filter (where a.status <> 'succeeded') as failures,
               round((count(*) filter (where a.status <> 'succeeded'))::numeric
                     / nullif(count(*),0), 4) as failure_rate
        from public.request_attempts a
        join public.providers p on p.id = a.provider_id
        where a.started_at >= now() - make_interval(mins => $1::int)
        group by 1, 2
        having count(*) >= $3::int
           and (count(*) filter (where a.status <> 'succeeded'))::numeric
               / nullif(count(*),0) >= $2::numeric
        """,
        ("window", "threshold", "floor"),
    ),
    "provider_unreachable": (
        "provider",
        """
        select a.provider_id::text as scope_id, p.name as label,
               count(*) as attempts,
               max(a.started_at) as last_attempt_at
        from public.request_attempts a
        join public.providers p on p.id = a.provider_id
        where a.started_at >= now() - make_interval(mins => $1::int)
        group by 1, 2
        having count(*) >= $3::int
           and count(*) filter (where a.status = 'succeeded') = 0
           and count(*) filter (
                 where a.error_category in ('provider_unavailable','timeout',
                                            'upstream_waf_rejection')
               ) >= $2::numeric
        """,
        ("window", "threshold", "floor"),
    ),
    "credential_pool_exhausted": (
        "provider",
        """
        select p.id::text as scope_id, p.name as label,
               count(c.id) as credentials,
               count(c.id) filter (where c.health in ('healthy','degraded')) as routable
        from public.providers p
        left join public.provider_credentials c on c.provider_id = p.id and c.enabled
        where p.enabled
        group by 1, 2
        having count(c.id) > 0
           and count(c.id) filter (where c.health in ('healthy','degraded')) <= $1::numeric
        """,
        ("threshold",),
    ),
    "model_no_eligible_route": (
        "model",
        """
        select r.requested_model as scope_id, r.requested_model as label,
               count(*) as occurrences, max(r.started_at) as last_seen_at
        from public.request_logs r
        where r.started_at >= now() - make_interval(mins => $1::int)
          and r.error_category = 'no_eligible_route'
        group by 1, 2 having count(*) >= $2::numeric
        """,
        ("window", "threshold"),
    ),
    "request_failure_rate": (
        "model",
        """
        select r.requested_model as scope_id, r.requested_model as label,
               count(*) as requests,
               count(*) filter (where r.status = 'failed') as failed,
               round((count(*) filter (where r.status = 'failed'))::numeric
                     / nullif(count(*),0), 4) as failure_rate
        from public.request_logs r
        where r.started_at >= now() - make_interval(mins => $1::int)
        group by 1, 2
        having count(*) >= $3::int
           and (count(*) filter (where r.status = 'failed'))::numeric
               / nullif(count(*),0) >= $2::numeric
        """,
        ("window", "threshold", "floor"),
    ),
    "cost_spike": (
        "global",
        """
        select 'global' as scope_id, 'All traffic' as label,
               round(coalesce(sum(u.estimated_cost),0)::numeric, 6) as window_cost,
               $2::numeric as threshold
        from public.usage_records u
        where u.recorded_at >= now() - make_interval(mins => $1::int)
          and u.estimated_cost is not null
        having coalesce(sum(u.estimated_cost),0) >= $2::numeric
        """,
        ("window", "threshold"),
    ),
    "budget_utilization": (
        "global",
        """
        select b.id::text as scope_id,
               b.name || ' (' || b.period || ')' as label,
               b.limit_amount as limit_amount,
               b.currency as currency,
               b.enforcement as enforcement,
               coalesce(w.reserved_cost, 0) as used,
               round((coalesce(w.reserved_cost,0) / nullif(b.limit_amount,0))::numeric, 4)
                 as utilization
        from public.gateway_budgets b
        left join public.budget_usage_windows w
          on w.budget_id = b.id
         and w.window_started_at = case b.period
               when 'daily' then date_trunc('day', now())
               else date_trunc('month', now()) end
        where b.enabled and b.limit_amount > 0
          and coalesce(w.reserved_cost,0) / b.limit_amount >= $1::numeric
        """,
        ("threshold",),
    ),
    "unpriced_traffic": (
        "global",
        """
        select 'global' as scope_id, 'All traffic' as label,
               count(*) as usage_records,
               count(*) filter (where u.estimated_cost is null) as unpriced,
               round((count(*) filter (where u.estimated_cost is null))::numeric
                     / nullif(count(*),0), 4) as unpriced_share
        from public.usage_records u
        where u.recorded_at >= now() - make_interval(mins => $1::int)
        having count(*) >= $3::int
           and (count(*) filter (where u.estimated_cost is null))::numeric
               / nullif(count(*),0) >= $2::numeric
        """,
        ("window", "threshold", "floor"),
    ),
}


@dataclass(frozen=True)
class MonitorResult:
    evaluated: int = 0
    raised: int = 0
    resolved: int = 0


def _threshold(condition: dict[str, Any]) -> float:
    """The single sensitivity value a condition compares against."""
    for key in ("at_least", "at_most", "threshold", "value"):
        if key in condition:
            try:
                return float(condition[key])
            except (TypeError, ValueError):
                continue
    return 0.0


async def evaluate_monitored_rules(pool: Any) -> MonitorResult:
    if pool is None or not hasattr(pool, "fetch"):
        return MonitorResult()
    try:
        rules = await pool.fetch(
            """select id,name,severity,scope_type,scope_id,condition,cooldown_seconds,
                      condition_kind,description,impact,recommended_action
               from public.alert_rules
               where enabled and condition_kind is not null"""
        )
    except Exception as exc:
        log_event(
            logger, logging.WARNING, "alert_monitor_rules_unavailable",
            error_type=type(exc).__name__,
        )
        return MonitorResult()

    evaluated = raised = resolved = 0
    for rule in rules:
        kind = rule["condition_kind"]
        spec = _CONDITIONS.get(kind)
        if spec is None:
            continue
        scope_type, query, bindings = spec
        condition = rule["condition"] or {}
        if isinstance(condition, str):
            condition = json.loads(condition)
        window = int(condition.get("window_minutes", 15) or 15)
        threshold = _threshold(condition)
        floor = int(condition.get("min_requests", condition.get("min_samples", 1)) or 1)
        available = {"window": window, "threshold": threshold, "floor": floor}
        try:
            rows = await pool.fetch(query, *(available[name] for name in bindings))
        except Exception as exc:
            log_event(
                logger, logging.WARNING, "alert_condition_failed",
                condition_kind=kind, error_type=type(exc).__name__,
            )
            continue
        evaluated += 1

        in_breach: list[str] = []
        for row in rows:
            observed = {
                key: value for key, value in dict(row).items()
                if key not in {"scope_id", "label"}
            }
            # Extra clauses on the rule still apply, so one condition can serve
            # several rules that differ only in what they additionally require.
            if not condition_matches(condition, observed):
                continue
            scope_id = str(row["scope_id"])
            if rule["scope_id"] and rule["scope_id"] != scope_id:
                continue
            dedup_key = f"{rule['id']}:{scope_type}:{scope_id}"
            in_breach.append(dedup_key)
            await raise_alert(
                pool,
                rule_id=rule["id"],
                dedup_key=dedup_key,
                severity=rule["severity"],
                event_type=kind,
                title=f"{rule['name']}: {row['label']}",
                scope_type=scope_type,
                scope_id=scope_id,
                metadata=observed,
                cooldown_seconds=rule["cooldown_seconds"],
                summary=rule["description"],
                impact=rule["impact"],
                recommended_action=rule["recommended_action"],
                observed=observed,
            )
            raised += 1

        # Anything this rule had open that is no longer in breach has recovered.
        try:
            stale = await pool.fetch(
                """select dedup_key from public.alerts
                   where rule_id = $1 and status in ('open','acknowledged')""",
                rule["id"],
            )
            clear = [
                row["dedup_key"] for row in stale if row["dedup_key"] not in set(in_breach)
            ]
            resolved += await resolve_alerts(pool, dedup_keys=clear, reason="recovered")
        except Exception as exc:
            log_event(
                logger, logging.WARNING, "alert_resolution_failed",
                condition_kind=kind, error_type=type(exc).__name__,
            )

    if raised or resolved:
        log_event(
            logger, logging.INFO, "alert_monitor_pass",
            evaluated=evaluated, raised=raised, resolved=resolved,
        )
    return MonitorResult(evaluated=evaluated, raised=raised, resolved=resolved)


async def alert_monitor_loop(interval_seconds: float, pool_getter) -> None:
    """Re-evaluate every monitored condition on a fixed cadence."""
    while True:
        await asyncio.sleep(interval_seconds)
        pool = pool_getter()
        if pool is None:
            continue
        try:
            await evaluate_monitored_rules(pool)
        except Exception as exc:
            log_event(
                logger, logging.WARNING, "alert_monitor_pass_failed",
                error_type=type(exc).__name__,
            )
