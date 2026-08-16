import json
from dataclasses import dataclass
from typing import Any


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
    if pool is None or currency is None or estimated_cost is None:
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
