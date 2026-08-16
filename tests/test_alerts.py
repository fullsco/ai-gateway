import json

import pytest

from gateway.alerts import AlertEvent, evaluate_alert_rules


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
