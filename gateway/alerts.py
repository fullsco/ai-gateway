import json
import logging
from dataclasses import dataclass, field
from typing import Any

from gateway.logging import log_event

logger = logging.getLogger("gateway.alerts")


@dataclass(frozen=True)
class AlertEvent:
    event_type: str
    title: str
    scopes: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, object] = field(default_factory=dict)


async def evaluate_alert_rules(pool: Any, event: AlertEvent) -> None:
    if pool is None or not hasattr(pool, "fetch"):
        return
    try:
        rules = await pool.fetch(
            """select id,severity,scope_type,scope_id,condition,cooldown_seconds
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
            if not _condition_matches(condition, event.metadata):
                continue
            alert_scope_type = scope_type or _primary_scope(event.scopes)[0]
            alert_scope_id = scope_id or _primary_scope(event.scopes)[1]
            dedup_key = ":".join(
                [str(rule["id"]), alert_scope_type or "global", alert_scope_id or "global"]
            )
            await pool.execute(
                """insert into public.alerts(
                     rule_id,dedup_key,severity,event_type,title,scope_type,scope_id,metadata)
                   values($1,$2,$3,$4,$5,$6,$7,$8::jsonb)
                   on conflict (dedup_key) where status in ('open','acknowledged')
                   do update set occurrence_count=alerts.occurrence_count+1,
                     last_seen_at=now(),metadata=excluded.metadata,title=excluded.title
                   where alerts.last_seen_at <= now()-make_interval(secs => $9)""",
                rule["id"],
                dedup_key,
                rule["severity"],
                event.event_type,
                event.title,
                alert_scope_type,
                alert_scope_id,
                json.dumps(event.metadata),
                rule["cooldown_seconds"],
            )
    except Exception as exc:
        log_event(
            logger,
            logging.WARNING,
            "alert_evaluation_failed",
            event_type=event.event_type,
            error_type=type(exc).__name__,
        )


def _condition_matches(condition: dict[str, object], metadata: dict[str, object]) -> bool:
    return all(metadata.get(key) == value for key, value in condition.items())


def _primary_scope(scopes: dict[str, str]) -> tuple[str | None, str | None]:
    for scope_type in ("credential", "route", "provider", "client", "model"):
        if scope_type in scopes:
            return scope_type, scopes[scope_type]
    return None, None
