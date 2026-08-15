import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from gateway.health import CircuitState
from gateway.routing import RouteControls


@pytest.mark.asyncio
async def test_circuit_opens_and_recovers_through_half_open_probe() -> None:
    controls = RouteControls()
    now = datetime(2026, 1, 1, tzinfo=UTC)

    for _ in range(3):
        assert await controls.allow("route", now=now)
        await controls.record_failure("route", now=now)

    assert controls.circuit_snapshot("route").state is CircuitState.OPEN
    assert not await controls.allow("route", now=now + timedelta(seconds=29))
    assert await controls.allow("route", now=now + timedelta(seconds=31))
    assert controls.circuit_snapshot("route").state is CircuitState.HALF_OPEN

    await controls.record_success("route")
    assert await controls.allow("route", now=now + timedelta(seconds=32))
    await controls.record_success("route")
    assert controls.circuit_snapshot("route").state is CircuitState.CLOSED


@pytest.mark.asyncio
async def test_concurrency_lease_blocks_until_released() -> None:
    controls = RouteControls({"route": 1})
    first = await controls.acquire("route", timeout_seconds=0.1)

    assert first is not None
    assert await controls.acquire("route", timeout_seconds=0.01) is None
    first.release()
    second = await controls.acquire("route", timeout_seconds=0.1)
    assert second is not None
    second.release()


@pytest.mark.asyncio
async def test_concurrency_lease_release_is_idempotent() -> None:
    controls = RouteControls({"route": 1})
    lease = await controls.acquire("route", timeout_seconds=0.1)
    assert lease is not None

    lease.release()
    lease.release()

    first = await controls.acquire("route", timeout_seconds=0.1)
    second = await controls.acquire("route", timeout_seconds=0.01)
    assert first is not None
    assert second is None
    first.release()


@pytest.mark.asyncio
async def test_waiting_acquisition_is_cancellation_safe() -> None:
    controls = RouteControls({"route": 1})
    lease = await controls.acquire("route", timeout_seconds=0.1)
    assert lease is not None
    waiting = asyncio.create_task(controls.acquire("route", timeout_seconds=60))
    await asyncio.sleep(0)

    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    lease.release()
    replacement = await controls.acquire("route", timeout_seconds=0.1)
    assert replacement is not None
    replacement.release()
