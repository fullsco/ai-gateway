"""The alert monitor must raise on breach and close on recovery.

Nothing previously set resolved_at, so an alert stayed open forever and the
operator could not tell a live problem from one that had already cleared.
"""

from typing import Any

import pytest

from gateway.alerts_monitor import _CONDITIONS, MonitorResult, evaluate_monitored_rules


def rule(**overrides: Any) -> dict[str, Any]:
    base = {
        "id": "rule-1",
        "name": "Provider failing",
        "severity": "critical",
        "scope_type": None,
        "scope_id": None,
        "condition": {"window_minutes": 15, "at_least": 0.5, "min_requests": 10},
        "cooldown_seconds": 300,
        "condition_kind": "provider_failure_rate",
        "description": "More than half of attempts to this provider failed.",
        "impact": "Requests are failing over, which adds latency and cost.",
        "recommended_action": "Check the provider status page and credential health.",
    }
    base.update(overrides)
    return base


class MonitorPool:
    """Answers rule lookup, the condition query, and the open-alert lookup."""

    def __init__(
        self,
        rules: list[dict[str, Any]],
        breaches: list[dict[str, Any]],
        open_alerts: list[str] | None = None,
    ) -> None:
        self.rules = rules
        self.breaches = breaches
        self.open_alerts = open_alerts or []
        self.raised: list[tuple[str, tuple[Any, ...]]] = []
        self.resolved: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if "from public.alert_rules" in query:
            return self.rules
        if "select dedup_key from public.alerts" in query:
            return [{"dedup_key": key} for key in self.open_alerts]
        if "update public.alerts set status='resolved'" in query:
            self.resolved.append((query, args))
            return [{"id": 1} for _ in args[0]]
        return self.breaches

    async def execute(self, query: str, *args: Any) -> None:
        if "insert into public.alerts" in query:
            self.raised.append((query, args))


@pytest.mark.asyncio
async def test_condition_in_breach_raises_an_alert_with_operator_guidance() -> None:
    pool = MonitorPool(
        [rule()],
        [{
            "scope_id": "provider-a",
            "label": "GoRouter",
            "attempts": 20,
            "failures": 18,
            "failure_rate": 0.9,
        }],
    )

    result = await evaluate_monitored_rules(pool)

    assert result.raised == 1
    _, args = pool.raised[0]
    assert args[1] == "rule-1:provider:provider-a"
    assert args[4] == "Provider failing: GoRouter"
    assert args[5] == "provider"
    assert args[6] == "provider-a"
    # The prose travels with the alert so it can be read without the rule.
    assert args[9] == "More than half of attempts to this provider failed."
    assert "failing over" in args[10]
    assert "status page" in args[11]


@pytest.mark.asyncio
async def test_a_condition_no_longer_in_breach_is_resolved() -> None:
    """This is what distinguishes a live problem from one that has recovered."""
    pool = MonitorPool([rule()], [], open_alerts=["rule-1:provider:provider-a"])

    result = await evaluate_monitored_rules(pool)

    assert result.raised == 0
    assert result.resolved == 1
    _, args = pool.resolved[0]
    assert args[0] == ["rule-1:provider:provider-a"]
    assert args[1] == "recovered"


@pytest.mark.asyncio
async def test_an_alert_still_in_breach_is_not_resolved() -> None:
    pool = MonitorPool(
        [rule()],
        [{"scope_id": "provider-a", "label": "GoRouter", "failure_rate": 0.9}],
        open_alerts=["rule-1:provider:provider-a"],
    )

    result = await evaluate_monitored_rules(pool)

    assert result.raised == 1
    assert result.resolved == 0


@pytest.mark.asyncio
async def test_rule_scoped_to_one_target_ignores_others() -> None:
    pool = MonitorPool(
        [rule(scope_id="provider-a")],
        [
            {"scope_id": "provider-a", "label": "A", "failure_rate": 0.9},
            {"scope_id": "provider-b", "label": "B", "failure_rate": 0.9},
        ],
    )

    result = await evaluate_monitored_rules(pool)

    assert result.raised == 1
    assert pool.raised[0][1][6] == "provider-a"


@pytest.mark.asyncio
async def test_extra_condition_clauses_further_narrow_a_shared_condition() -> None:
    """One condition serves several rules that differ in what they also require."""
    pool = MonitorPool(
        [rule(condition={"window_minutes": 15, "at_least": 0.5, "attempts": {"at_least": 50}})],
        [{"scope_id": "provider-a", "label": "A", "attempts": 20, "failure_rate": 0.9}],
    )

    result = await evaluate_monitored_rules(pool)

    assert result.raised == 0, "20 attempts must not satisfy an at_least of 50"


@pytest.mark.asyncio
async def test_unknown_condition_kind_is_skipped_not_fatal() -> None:
    pool = MonitorPool([rule(condition_kind="not_a_real_condition")], [])

    assert await evaluate_monitored_rules(pool) == MonitorResult()


@pytest.mark.asyncio
async def test_monitor_survives_an_unavailable_database() -> None:
    class BrokenPool:
        async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
            raise RuntimeError("database unavailable")

    assert await evaluate_monitored_rules(BrokenPool()) == MonitorResult()


def test_every_declared_condition_kind_has_a_query() -> None:
    """A rule whose kind has no query would silently never fire."""
    allowed = {
        "credential_quota_low",
        "credential_balance_low",
        "credential_auth_failures",
        "provider_failure_rate",
        "provider_unreachable",
        "model_no_eligible_route",
        "credential_pool_exhausted",
        "request_failure_rate",
        "cost_spike",
        "unpriced_traffic",
    }

    assert set(_CONDITIONS) == allowed
