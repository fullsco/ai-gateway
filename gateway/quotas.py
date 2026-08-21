import json
import logging
from dataclasses import dataclass
from typing import Any

from gateway.logging import log_event

logger = logging.getLogger("gateway.quotas")


class QuotaExceeded(RuntimeError):
    def __init__(self, quota: str) -> None:
        super().__init__(f"Client {quota} quota exceeded.")
        self.quota = quota


class QuotaUnavailable(RuntimeError):
    pass


class ProviderQuotaExceeded(RuntimeError):
    pass


class BudgetExceeded(RuntimeError):
    pass


class UnpricedRouteBlocked(BudgetExceeded):
    """A blocking budget applies but this route has no pricing to charge against.

    Budget reservation is driven by an estimated cost, so a route with no pricing
    reserved nothing and was invisible to every budget, including a global
    blocking one. A spend cap must not be evadable by leaving pricing empty, so
    the route is refused instead. It is reported as a budget failure so the
    executor excludes it and tries another, priced route.
    """


@dataclass(frozen=True)
class QuotaRequest:
    requests_per_minute: int | None
    tokens_per_minute: int | None
    estimated_tokens: int


def estimate_tokens(payload: dict[str, Any]) -> int:
    """Conservative request estimate used before upstream usage is available."""
    return max(1, (len(json.dumps(payload, separators=(",", ":"), ensure_ascii=True)) + 3) // 4)


async def reserve_client_quota(pool: Any, client_id: str, request: QuotaRequest) -> None:
    if pool is None or not any(
        value is not None
        for value in (
            request.requests_per_minute,
            request.tokens_per_minute,
        )
    ):
        return
    try:
        result = await pool.fetchval(
            "select public.reserve_client_quota($1::uuid,$2::integer,$3::bigint,$4::bigint)",
            client_id,
            request.requests_per_minute,
            request.tokens_per_minute,
            request.estimated_tokens,
        )
    except Exception as exc:
        raise QuotaUnavailable("Client quota enforcement is unavailable.") from exc
    if result:
        raise QuotaExceeded(str(result))


async def reserve_provider_quota(
    pool: Any,
    credential_id: str,
    request: QuotaRequest,
) -> None:
    if pool is None or not any(
        value is not None
        for value in (request.requests_per_minute, request.tokens_per_minute)
    ):
        return
    try:
        result = await pool.fetchval(
            "select public.reserve_provider_credential_quota($1::uuid,$2,$3,$4)",
            credential_id,
            request.requests_per_minute,
            request.tokens_per_minute,
            request.estimated_tokens,
        )
    except Exception as exc:
        raise QuotaUnavailable("Provider quota enforcement is unavailable.") from exc
    if result:
        raise ProviderQuotaExceeded(str(result))


class ClientSpendingLimitExceeded(BudgetExceeded):
    """The client's own monthly spending limit would be exceeded.

    Reported as a budget failure so the executor excludes the route and tries
    another, but the limit is the client's rather than an operator budget, so it
    is distinguished for the operator-facing message.
    """


async def reserve_client_spending(
    pool: Any, client_id: str, estimated_cost: object | None
) -> None:
    """Hold spend against gateway_clients.spending_limit.

    The limit was previously stored, published and displayed but never read, so a
    client spend cap had no effect at all.
    """
    if pool is None or estimated_cost is None:
        return
    try:
        result = await pool.fetchval(
            "select public.reserve_client_spending($1::uuid,$2::numeric)",
            client_id,
            estimated_cost,
        )
    except Exception as exc:
        raise QuotaUnavailable("Client spending enforcement is unavailable.") from exc
    if result:
        raise ClientSpendingLimitExceeded(
            "This client has reached its configured monthly spending limit."
        )


# An unpriced route cannot be charged, so it cannot be held below a limit. Refusing
# it outright, however, takes a working model offline for an accounting reason: a
# provider whose billing units cannot be interpreted, or whose rates are not
# published, would be permanently unusable. The proportionate rule is to let it run
# while the budget still has headroom - the unpriced_traffic alert tells the operator
# to fix the rate card - and to refuse it only once a blocking budget is already at
# its limit, so unmeasurable spend can never carry a budget past its cap.
_EXHAUSTED_BLOCKING_BUDGET_FOR_SCOPE = """
select b.name from public.gateway_budgets b
left join public.budget_usage_windows w
  on w.budget_id = b.id
 and w.window_started_at = case b.period
       when 'daily' then date_trunc('day', now())
       else date_trunc('month', now()) end
where b.enabled and b.enforcement = 'block' and (
  b.scope_type = 'global'
  or (b.scope_type = 'client' and b.scope_id = $1)
  or (b.scope_type = 'provider' and b.scope_id = $2)
  or (b.scope_type = 'credential' and b.scope_id = $3)
  or (b.scope_type = 'model' and b.scope_id = $4)
  or (b.scope_type = 'route' and b.scope_id = $5)
)
  and coalesce(w.reserved_cost, 0) >= b.limit_amount
order by b.id
limit 1
"""


async def reserve_budgets(
    pool: Any,
    *,
    client_id: str,
    provider_id: str,
    credential_id: str,
    model_id: str,
    route_id: str | None,
    currency: str | None,
    estimated_cost: object | None,
) -> None:
    if pool is None:
        return
    if currency is None or estimated_cost is None:
        # No cost could be derived, so nothing can be reserved. Only refuse the
        # route when a blocking budget actually applies to it; a deployment with
        # no budgets configured is unaffected. Currency is not part of the match
        # because an unpriced route has no currency to compare.
        try:
            blocking = await pool.fetchval(
                _EXHAUSTED_BLOCKING_BUDGET_FOR_SCOPE,
                client_id,
                provider_id,
                credential_id,
                model_id,
                route_id,
            )
        except Exception:
            # This guard exists to stop a spend cap being evaded by omitted
            # pricing, which is a configuration state, not an outage. A database
            # outage cannot be an evasion, and priced routes already fail closed
            # through reserve_gateway_budgets below, so preserve availability
            # here rather than turning a telemetry outage into an inference one.
            log_event(
                logger,
                logging.WARNING,
                "unpriced_budget_guard_unavailable",
                provider_id=provider_id,
                model_id=model_id,
            )
            return
        if blocking:
            raise UnpricedRouteBlocked(
                f"Budget '{blocking}' has reached its limit, and this route has no "
                "pricing configured, so its spend cannot be measured or charged "
                "against the budget."
            )
        return
    try:
        result = await pool.fetchval(
            "select public.reserve_gateway_budgets($1::uuid,$2::uuid,$3::uuid,$4,$5::uuid,$6,$7)",
            client_id,
            provider_id,
            credential_id,
            model_id,
            route_id,
            currency,
            estimated_cost,
        )
    except Exception as exc:
        raise QuotaUnavailable("Budget enforcement is unavailable.") from exc
    if result:
        raise BudgetExceeded(str(result))


async def settle_budgets(
    pool: Any,
    *,
    client_id: str,
    provider_id: str,
    credential_id: str,
    model_id: str,
    route_id: str | None,
    currency: str | None,
    delta: object | None,
) -> None:
    """Correct a reservation to what the request actually cost.

    Reservation happens before dispatch from an estimate whose output side is the
    client's declared max_tokens, not its outcome. Left uncorrected, reserved spend
    drifts permanently above real spend and a budget refuses traffic long before
    its limit is genuinely reached.

    Failures here are logged and swallowed: a correction that does not land leaves
    the budget conservative, which is the safe direction, and must never fail a
    request that already succeeded upstream.
    """
    if pool is None or currency is None or delta in (None, 0):
        return
    try:
        await pool.execute(
            "select public.settle_gateway_budgets"
            "($1::uuid,$2::uuid,$3::uuid,$4,$5::uuid,$6,$7)",
            client_id,
            provider_id,
            credential_id,
            model_id,
            route_id,
            currency,
            delta,
        )
    except Exception:
        log_event(
            logger,
            logging.WARNING,
            "budget_settlement_failed",
            provider_id=provider_id,
            model_id=model_id,
        )


async def settle_client_spending(pool: Any, client_id: str, delta: object | None) -> None:
    """Correct a client spending reservation to the actual cost."""
    if pool is None or delta in (None, 0):
        return
    try:
        await pool.execute(
            "select public.settle_client_spending($1::uuid,$2::numeric)", client_id, delta
        )
    except Exception:
        log_event(
            logger, logging.WARNING, "client_spend_settlement_failed", client_id=client_id
        )
