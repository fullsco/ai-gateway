import json
import logging
from dataclasses import dataclass, field
from typing import Any

from gateway.logging import log_event

logger = logging.getLogger("gateway.alerts")

# Comparison operators a rule may use. Exact equality was the only thing
# supported, which made every threshold condition inexpressible: "failure rate
# above half" or "five auth failures in ten minutes" could not be written down.
_OPERATORS = {
    "equals": lambda observed, target: observed == target,
    "not_equals": lambda observed, target: observed != target,
    "at_least": lambda observed, target: _numeric(observed) is not None
    and _numeric(observed) >= float(target),
    "at_most": lambda observed, target: _numeric(observed) is not None
    and _numeric(observed) <= float(target),
    "greater_than": lambda observed, target: _numeric(observed) is not None
    and _numeric(observed) > float(target),
    "less_than": lambda observed, target: _numeric(observed) is not None
    and _numeric(observed) < float(target),
    "one_of": lambda observed, target: isinstance(target, list) and observed in target,
    "contains": lambda observed, target: isinstance(observed, str)
    and str(target).lower() in observed.lower(),
}

# Thresholds describe how to evaluate a condition rather than what to match in
# the payload, so they are not treated as metadata comparisons. The sensitivity
# keys are applied by the monitor's SQL; re-comparing them against the observed
# row would look for a column literally named "at_least" and never match.
_CONTROL_KEYS = frozenset(
    {
        "window_minutes",
        "min_requests",
        "min_samples",
        "baseline_days",
        "at_least",
        "at_most",
        "threshold",
        "value",
    }
)


@dataclass(frozen=True)
class AlertEvent:
    event_type: str
    title: str
    scopes: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, object] = field(default_factory=dict)
    # Prose carried with the alert so the operator is told what happened, why it
    # matters and what to do, without needing the schema or the rule definition.
    summary: str | None = None
    impact: str | None = None
    recommended_action: str | None = None
    observed: dict[str, object] = field(default_factory=dict)


def _numeric(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def evaluate_alert_rules(pool: Any, event: AlertEvent) -> None:
    if pool is None or not hasattr(pool, "fetch"):
        return
    try:
        rules = await pool.fetch(
            """select id,severity,scope_type,scope_id,condition,cooldown_seconds,
                      description,impact,recommended_action
               from public.alert_rules where enabled and event_type=$1""",
            event.event_type,
        )
        for rule in rules:
            scope_type = rule["scope_type"]
            scope_id = rule["scope_id"]
            if scope_type and event.scopes.get(scope_type) != scope_id:
                continue
            condition = rule["condition"] or {}
            if isinstance(condition, str):
                condition = json.loads(condition)
            if not condition_matches(condition, event.metadata):
                continue
            alert_scope_type = scope_type or _primary_scope(event.scopes)[0]
            alert_scope_id = scope_id or _primary_scope(event.scopes)[1]
            await raise_alert(
                pool,
                rule_id=rule["id"],
                dedup_key=":".join(
                    [str(rule["id"]), alert_scope_type or "global", alert_scope_id or "global"]
                ),
                severity=rule["severity"],
                event_type=event.event_type,
                title=event.title,
                scope_type=alert_scope_type,
                scope_id=alert_scope_id,
                metadata=event.metadata,
                cooldown_seconds=rule["cooldown_seconds"],
                summary=event.summary or rule["description"],
                impact=event.impact or rule["impact"],
                recommended_action=event.recommended_action or rule["recommended_action"],
                observed=event.observed or event.metadata,
            )
    except Exception as exc:
        log_event(
            logger,
            logging.WARNING,
            "alert_evaluation_failed",
            event_type=event.event_type,
            error_type=type(exc).__name__,
        )


async def raise_alert(
    pool: Any,
    *,
    rule_id: Any,
    dedup_key: str,
    severity: str,
    event_type: str,
    title: str,
    scope_type: str | None,
    scope_id: str | None,
    metadata: dict[str, object],
    cooldown_seconds: int,
    summary: str | None = None,
    impact: str | None = None,
    recommended_action: str | None = None,
    observed: dict[str, object] | None = None,
) -> None:
    """Open an alert, or fold it into the open one for the same condition."""
    await pool.execute(
        """insert into public.alerts(
             rule_id,dedup_key,severity,event_type,title,scope_type,scope_id,metadata,
             summary,impact,recommended_action,observed)
           values($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$10,$11,$12,$13::jsonb)
           on conflict (dedup_key) where status in ('open','acknowledged')
           do update set occurrence_count=alerts.occurrence_count+1,
             last_seen_at=now(),metadata=excluded.metadata,title=excluded.title,
             observed=excluded.observed
           where alerts.last_seen_at <= now()-make_interval(secs => $9)""",
        rule_id,
        dedup_key,
        severity,
        event_type,
        title,
        scope_type,
        scope_id,
        json.dumps(metadata, default=str),
        cooldown_seconds,
        summary,
        impact,
        recommended_action,
        json.dumps(observed or {}, default=str),
    )


async def resolve_alerts(
    pool: Any,
    *,
    dedup_keys: list[str],
    reason: str = "recovered",
) -> int:
    """Close alerts whose condition no longer holds.

    Without this every alert stayed open indefinitely, so the operator could not
    distinguish a live problem from one that had already cleared.
    """
    if pool is None or not dedup_keys:
        return 0
    rows = await pool.fetch(
        """update public.alerts set status='resolved', resolved_at=now(),
                  resolved_reason=$2
           where dedup_key = any($1::text[]) and status in ('open','acknowledged')
           returning id""",
        dedup_keys,
        reason,
    )
    return len(rows)


def condition_matches(condition: dict[str, object], metadata: dict[str, object]) -> bool:
    """True when every clause in the condition holds against the observed values.

    A bare value is an equality check, which keeps older rules working. A mapping
    selects a named operator, so thresholds read as
    ``{"failure_rate": {"at_least": 0.5}}``.
    """
    for key, expected in condition.items():
        if key in _CONTROL_KEYS:
            continue
        observed = metadata.get(key)
        if isinstance(expected, dict):
            for operator, target in expected.items():
                check = _OPERATORS.get(operator)
                if check is None or not check(observed, target):
                    return False
        elif observed != expected:
            return False
    return True


def _primary_scope(scopes: dict[str, str]) -> tuple[str | None, str | None]:
    for scope_type in ("credential", "route", "provider", "client", "model"):
        if scope_type in scopes:
            return scope_type, scopes[scope_type]
    return None, None
