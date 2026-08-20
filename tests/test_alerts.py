import json

import pytest

from gateway.alerts import (
    AlertEvent,
    condition_matches,
    evaluate_alert_rules,
    resolve_alerts,
)


class AlertPool:
    def __init__(self, rules=None, *, fail=False) -> None:
        self.rules = rules or []
        self.fail = fail
        self.fetch_calls = []
        self.execute_calls = []

    async def fetch(self, query, *args):
        self.fetch_calls.append((query, args))
        if self.fail:
            raise RuntimeError("database unavailable")
        return self.rules

    async def execute(self, query, *args):
        self.execute_calls.append((query, args))


@pytest.mark.asyncio
async def test_alert_rule_matches_event_scope_and_condition() -> None:
    pool = AlertPool(
        [{
            "id": "rule-1",
            "severity": "critical",
            "scope_type": "provider",
            "scope_id": "provider-a",
            "condition": {"error_category": "rate_limit"},
            "cooldown_seconds": 300,
            "description": "A provider rate limited a credential.",
            "impact": "Traffic moves to another credential; sustained limiting reduces capacity.",
            "recommended_action": "Add credentials or lower concurrency if it persists.",
        }]
    )

    await evaluate_alert_rules(
        pool,
        AlertEvent(
            event_type="provider_failure",
            title="Provider request failed: rate_limit",
            scopes={"provider": "provider-a", "credential": "credential-a"},
            metadata={"error_category": "rate_limit", "upstream_status": 429},
        ),
    )

    assert pool.fetch_calls[0][1] == ("provider_failure",)
    query, args = pool.execute_calls[0]
    assert "on conflict (dedup_key)" in query
    assert args[1] == "rule-1:provider:provider-a"
    assert args[2:7] == (
        "critical",
        "provider_failure",
        "Provider request failed: rate_limit",
        "provider",
        "provider-a",
    )
    assert json.loads(args[7]) == {
        "error_category": "rate_limit",
        "upstream_status": 429,
    }
    assert args[8] == 300


@pytest.mark.asyncio
async def test_alert_rule_ignores_scope_or_condition_mismatch() -> None:
    pool = AlertPool(
        [
            {
                "id": "rule-1",
                "severity": "warning",
                "scope_type": "provider",
                "scope_id": "provider-b",
                "condition": {},
                "cooldown_seconds": 0,
            },
            {
                "id": "rule-2",
                "severity": "warning",
                "scope_type": None,
                "scope_id": None,
                "condition": {"reason": "tokens"},
                "cooldown_seconds": 0,
            },
        ]
    )

    await evaluate_alert_rules(
        pool,
        AlertEvent(
            event_type="provider_quota",
            title="Provider quota exceeded",
            scopes={"provider": "provider-a"},
            metadata={"reason": "requests"},
        ),
    )

    assert pool.execute_calls == []


@pytest.mark.asyncio
async def test_alert_evaluation_fails_open() -> None:
    await evaluate_alert_rules(
        AlertPool(fail=True),
        AlertEvent(event_type="provider_failure", title="Failure"),
    )


# --- condition operators -----------------------------------------------------


@pytest.mark.parametrize(
    ("condition", "observed", "expected"),
    [
        ({"failure_rate": {"at_least": 0.5}}, {"failure_rate": 0.6}, True),
        ({"failure_rate": {"at_least": 0.5}}, {"failure_rate": 0.4}, False),
        ({"routable": {"at_most": 0}}, {"routable": 0}, True),
        ({"routable": {"at_most": 0}}, {"routable": 2}, False),
        ({"failures": {"greater_than": 3}}, {"failures": 4}, True),
        ({"failures": {"greater_than": 3}}, {"failures": 3}, False),
        ({"status": {"one_of": ["failed", "cancelled"]}}, {"status": "failed"}, True),
        ({"status": {"one_of": ["failed"]}}, {"status": "succeeded"}, False),
        ({"message": {"contains": "balance"}}, {"message": "Insufficient BALANCE"}, True),
        # A bare value stays an equality check so older rules keep working.
        ({"error_category": "rate_limit"}, {"error_category": "rate_limit"}, True),
        ({"error_category": "rate_limit"}, {"error_category": "timeout"}, False),
        # Control keys describe how to evaluate, not what to match.
        ({"window_minutes": 15, "min_requests": 20}, {}, True),
        # A missing value cannot satisfy a threshold.
        ({"failure_rate": {"at_least": 0.5}}, {}, False),
    ],
)
def test_condition_operators(condition, observed, expected) -> None:
    """Exact equality was the only operator, so thresholds were inexpressible."""
    assert condition_matches(condition, observed) is expected


@pytest.mark.asyncio
async def test_resolving_alerts_closes_only_open_ones() -> None:
    class ResolvePool:
        def __init__(self) -> None:
            self.calls = []

        async def fetch(self, query, *args):
            self.calls.append((query, args))
            return [{"id": 1}, {"id": 2}]

    pool = ResolvePool()
    closed = await resolve_alerts(pool, dedup_keys=["a", "b"], reason="recovered")

    assert closed == 2
    query, args = pool.calls[0]
    assert "status='resolved'" in query.replace(" ", "")
    assert "status in ('open','acknowledged')" in query
    assert args == (["a", "b"], "recovered")


@pytest.mark.asyncio
async def test_resolving_nothing_is_a_no_op() -> None:
    assert await resolve_alerts(None, dedup_keys=["a"]) == 0
    assert await resolve_alerts(object(), dedup_keys=[]) == 0
