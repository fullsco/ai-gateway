from decimal import Decimal

import pytest

from gateway.quotas import (
    BudgetExceeded,
    ClientSpendingLimitExceeded,
    QuotaExceeded,
    QuotaRequest,
    QuotaUnavailable,
    UnpricedRouteBlocked,
    estimate_tokens,
    reserve_budgets,
    reserve_client_quota,
    reserve_client_spending,
    settle_budgets,
    settle_client_spending,
)


class QuotaPool:
    def __init__(self, result=None) -> None:
        self.result = result
        self.calls = []

    async def fetchval(self, query, *args):
        self.calls.append((query, args))
        return self.result


def test_estimate_tokens_is_positive_and_payload_only() -> None:
    assert estimate_tokens({"messages": [{"role": "user", "content": "hello"}]}) > 0


@pytest.mark.asyncio
async def test_reserve_client_quota_calls_atomic_database_function() -> None:
    pool = QuotaPool()
    await reserve_client_quota(
        pool,
        "client-1",
        QuotaRequest(10, 1000, 12),
    )

    assert "reserve_client_quota" in pool.calls[0][0]
    assert pool.calls[0][1] == ("client-1", 10, 1000, 12)


@pytest.mark.asyncio
async def test_unlimited_client_does_not_query_database() -> None:
    pool = QuotaPool("unexpected")
    await reserve_client_quota(pool, "client-1", QuotaRequest(None, None, 1))

    assert pool.calls == []


@pytest.mark.asyncio
async def test_reserve_client_quota_maps_database_rejection() -> None:
    with pytest.raises(QuotaExceeded, match="rate quota"):
        await reserve_client_quota(
            QuotaPool("rate quota"),
            "client-1",
            QuotaRequest(1, None, 1),
        )


@pytest.mark.asyncio
async def test_limited_client_fails_closed_when_database_is_unavailable() -> None:
    class FailingPool(QuotaPool):
        async def fetchval(self, query, *args):
            raise RuntimeError("database unavailable")

    with pytest.raises(QuotaUnavailable, match="enforcement is unavailable"):
        await reserve_client_quota(
            FailingPool(),
            "client-1",
            QuotaRequest(1, None, 1),
        )


class BudgetScopePool:
    """Fake pool answering the blocking-budget scope lookup."""

    def __init__(self, blocking_name: str | None) -> None:
        self.blocking_name = blocking_name
        self.calls: list[tuple[str, tuple]] = []

    async def fetchval(self, query, *args):
        self.calls.append((query, args))
        if "gateway_budgets" in query and "enforcement = 'block'" in query:
            return self.blocking_name
        return None


def _scopes() -> dict[str, str | None]:
    return {
        "client_id": "11111111-1111-1111-1111-111111111111",
        "provider_id": "22222222-2222-2222-2222-222222222222",
        "credential_id": "33333333-3333-3333-3333-333333333333",
        "model_id": "claude-opus-5",
        "route_id": "44444444-4444-4444-4444-444444444444",
    }


@pytest.mark.asyncio
async def test_unpriced_route_is_refused_once_a_blocking_budget_is_exhausted() -> None:
    """Unmeasurable spend must not carry a budget past its cap.

    An unpriced route reserves nothing, so it was invisible to every budget
    including a global blocking one, and a cap could be evaded by leaving pricing
    empty.
    """
    pool = BudgetScopePool("Global monthly cap")

    with pytest.raises(UnpricedRouteBlocked, match="reached its limit"):
        await reserve_budgets(pool, currency=None, estimated_cost=None, **_scopes())


@pytest.mark.asyncio
async def test_unpriced_route_runs_while_the_budget_still_has_headroom() -> None:
    """Refusing it outright would take a working model offline to satisfy accounting.

    A provider whose billing units cannot be interpreted would otherwise be
    permanently unusable. The unpriced_traffic alert is what tells the operator to
    fix the rate card.
    """
    pool = BudgetScopePool(None)

    await reserve_budgets(pool, currency=None, estimated_cost=None, **_scopes())

    assert pool.calls, "the exhausted-budget check must still run"
    assert "reserved_cost" in pool.calls[0][0], (
        "the check must compare spend against the limit, not merely find a budget"
    )


@pytest.mark.asyncio
async def test_priced_route_reserves_against_the_budget_function() -> None:
    pool = BudgetScopePool(None)

    await reserve_budgets(pool, currency="USD", estimated_cost=Decimal("0.25"), **_scopes())

    assert any("reserve_gateway_budgets" in query for query, _ in pool.calls)


@pytest.mark.asyncio
async def test_budget_over_limit_raises_budget_exceeded() -> None:
    class OverLimitPool(BudgetScopePool):
        async def fetchval(self, query, *args):
            self.calls.append((query, args))
            if "reserve_gateway_budgets" in query:
                return "budget-id-1"
            return None

    with pytest.raises(BudgetExceeded):
        await reserve_budgets(
            OverLimitPool(None), currency="USD", estimated_cost=Decimal("5"), **_scopes()
        )


@pytest.mark.asyncio
async def test_unpriced_guard_failure_does_not_break_inference() -> None:
    """A database outage is not an evasion of a spend cap.

    Priced routes still fail closed through reserve_gateway_budgets, so keeping
    availability here avoids turning a telemetry outage into an inference outage.
    """

    class BrokenPool:
        async def fetchval(self, query, *args):
            raise RuntimeError("database unavailable")

    await reserve_budgets(BrokenPool(), currency=None, estimated_cost=None, **_scopes())


class SpendPool:
    def __init__(self, result=None) -> None:
        self.result = result
        self.calls: list[tuple[str, tuple]] = []

    async def fetchval(self, query, *args):
        self.calls.append((query, args))
        return self.result


@pytest.mark.asyncio
async def test_client_spending_limit_is_actually_enforced() -> None:
    """spending_limit was stored, published and displayed but never read."""
    pool = SpendPool("spending_limit")

    with pytest.raises(ClientSpendingLimitExceeded, match="monthly spending limit"):
        await reserve_client_spending(pool, "client-1", Decimal("0.50"))

    assert "reserve_client_spending" in pool.calls[0][0]


@pytest.mark.asyncio
async def test_client_within_spending_limit_proceeds() -> None:
    pool = SpendPool(None)

    await reserve_client_spending(pool, "client-1", Decimal("0.50"))

    assert pool.calls, "the reservation must be attempted"


@pytest.mark.asyncio
async def test_unpriced_request_does_not_reserve_client_spend() -> None:
    """An unpriced request is refused by the unpriced-route guard, not here."""
    pool = SpendPool(None)

    await reserve_client_spending(pool, "client-1", None)

    assert pool.calls == []


@pytest.mark.asyncio
async def test_client_spending_enforcement_failure_fails_closed() -> None:
    class BrokenPool:
        async def fetchval(self, query, *args):
            raise RuntimeError("database unavailable")

    with pytest.raises(QuotaUnavailable):
        await reserve_client_spending(BrokenPool(), "client-1", Decimal("0.50"))


@pytest.mark.asyncio
async def test_settlement_corrects_a_reservation_to_the_actual_cost() -> None:
    """A reservation prices output at the client's max_tokens, not its outcome.

    Measured on live traffic before settlement existed: $9.285266 reserved against
    $4.931146 spent, a factor of 1.88, which would have made a $4,000 budget refuse
    requests at roughly $2,124 of real spend.
    """

    class SettlePool:
        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple]] = []

        async def execute(self, query, *args):
            self.calls.append((query, args))

    pool = SettlePool()
    await settle_budgets(
        pool, currency="USD", delta=Decimal("-1.20"), **_scopes()
    )

    assert "settle_gateway_budgets" in pool.calls[0][0]
    assert pool.calls[0][1][-1] == Decimal("-1.20")


@pytest.mark.asyncio
async def test_settlement_is_skipped_when_there_is_nothing_to_correct() -> None:
    class SettlePool:
        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple]] = []

        async def execute(self, query, *args):
            self.calls.append((query, args))

    pool = SettlePool()
    await settle_budgets(pool, currency="USD", delta=0, **_scopes())
    await settle_budgets(pool, currency="USD", delta=None, **_scopes())
    await settle_budgets(pool, currency=None, delta=Decimal("1"), **_scopes())

    assert pool.calls == []


@pytest.mark.asyncio
async def test_a_failed_settlement_never_fails_the_request() -> None:
    """The request already succeeded upstream; an uncorrected budget is merely
    conservative, which is the safe direction."""

    class BrokenPool:
        async def execute(self, query, *args):
            raise RuntimeError("database unavailable")

    await settle_budgets(
        BrokenPool(), currency="USD", delta=Decimal("-1"), **_scopes()
    )
    await settle_client_spending(BrokenPool(), "client-1", Decimal("-1"))
