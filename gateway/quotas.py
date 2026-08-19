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


_BLOCKING_BUDGET_FOR_SCOPE = """
select b.name from public.gateway_budgets b
where b.enabled and b.enforcement = 'block' and (
  b.scope_type = 'global'
  or (b.scope_type = 'client' and b.scope_id = $1)
  or (b.scope_type = 'provider' and b.scope_id = $2)
  or (b.scope_type = 'credential' and b.scope_id = $3)
  or (b.scope_type = 'model' and b.scope_id = $4)
  or (b.scope_type = 'route' and b.scope_id = $5)
)
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
                _BLOCKING_BUDGET_FOR_SCOPE,
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
                f"Budget '{blocking}' blocks spending on this route because the route "
                "has no pricing configured, so its cost cannot be measured."
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
