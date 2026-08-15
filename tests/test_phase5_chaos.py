from datetime import UTC, datetime, timedelta

import pytest

from gateway.health import CircuitState
from gateway.routing import RouteControls


@pytest.mark.asyncio
async def test_chaos_route_failure_exclusion_and_recovery() -> None:
    controls = RouteControls()
    now = datetime(2026, 1, 1, tzinfo=UTC)

    for _ in range(3):
        assert await controls.allow("primary", now=now)
        await controls.record_failure("primary", now=now)

    assert controls.circuit_snapshot("primary").state is CircuitState.OPEN
    assert not await controls.allow("primary", now=now + timedelta(seconds=1))
    assert await controls.allow("fallback", now=now + timedelta(seconds=1))
    await controls.record_success("fallback")

    assert await controls.allow("primary", now=now + timedelta(seconds=31))
    await controls.record_success("primary")
    await controls.record_success("primary")
    assert controls.circuit_snapshot("primary").state is CircuitState.CLOSED


@pytest.mark.asyncio
async def test_chaos_concurrency_saturation_does_not_affect_other_route() -> None:
    controls = RouteControls({"primary": 1, "fallback": 1})
    primary = await controls.acquire("primary", timeout_seconds=0.1)
    assert primary is not None

    assert await controls.acquire("primary", timeout_seconds=0.01) is None
    fallback = await controls.acquire("fallback", timeout_seconds=0.1)
    assert fallback is not None

    primary.release()
    fallback.release()
