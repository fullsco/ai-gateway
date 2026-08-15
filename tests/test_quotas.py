import pytest

from gateway.quotas import (
    QuotaExceeded,
    QuotaRequest,
    QuotaUnavailable,
    estimate_tokens,
    reserve_client_quota,
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
