import asyncio
import gc
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


@pytest.mark.asyncio
async def test_reconfiguration_preserves_active_leases_and_honours_reduced_limit():
    controls = RouteControls({"route": 2})
    first = await controls.acquire("route", timeout_seconds=.1)
    second = await controls.acquire("route", timeout_seconds=.1)
    controls.reconfigure({"route": 1})
    assert await controls.acquire("route", timeout_seconds=.01) is None
    first.release()
    assert await controls.acquire("route", timeout_seconds=.01) is None
    second.release()
    replacement = await controls.acquire("route", timeout_seconds=.1)
    assert replacement is not None
    replacement.release()


@pytest.mark.asyncio
async def test_unowned_removed_route_state_is_pruned_on_reconfigure():
    controls = RouteControls({"stable": 1})
    await controls.record_failure("stable")
    circuit = controls.circuit_snapshot("stable")
    for version in range(25):
        route_id = f"route-{version}"
        controls.reconfigure({"stable": 1, route_id: 1})
        await controls.record_failure(route_id)
        assert set(controls._limits) == {"stable", route_id}
        assert set(controls._circuits) == {"stable", route_id}
        assert controls.circuit_snapshot("stable") is circuit
    controls.reconfigure({"stable": 1})
    assert set(controls._limits) == set(controls._circuits) == {"stable"}


@pytest.mark.asyncio
async def test_waiting_acquisition_pins_removed_route_until_last_lease_is_gone():
    controls = RouteControls({"removed": 1})
    first = await controls.acquire("removed", timeout_seconds=.1)
    assert first is not None
    await controls.record_failure("removed")
    waiting = asyncio.create_task(controls.acquire("removed", timeout_seconds=1))
    await asyncio.sleep(0)
    assert len(controls._owners) == 2
    controls.reconfigure({})
    first.release()
    del first
    gc.collect()
    assert controls._limits == {"removed": 1}
    second = await waiting
    assert second is not None
    assert controls._active == {"removed": 1}
    second.release()
    await controls.record_success("removed")
    assert "removed" in controls._circuits
    del second, waiting
    # Completed tasks retain their result until the task is collected.
    await asyncio.sleep(0)
    gc.collect()
    assert controls._limits == controls._circuits == controls._active == {}
    assert controls._owners == controls._route_owners == {}


@pytest.mark.asyncio
async def test_cancelled_and_timed_out_waiters_do_not_leak_route_ownership():
    controls = RouteControls({"removed": 1})
    lease = await controls.acquire("removed", timeout_seconds=.1)
    assert lease is not None
    waiting = asyncio.create_task(controls.acquire("removed", timeout_seconds=60))
    await asyncio.sleep(0)
    controls.reconfigure({})
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert await controls.acquire("removed", timeout_seconds=.01) is None
    assert len(controls._owners) == 1
    lease.release()
    del lease
    gc.collect()
    assert controls._limits == controls._active == {}
    assert controls._owners == controls._route_owners == {}


@pytest.mark.asyncio
async def test_reintroduced_route_keeps_state_while_old_lease_is_held():
    controls = RouteControls({"route": 1})
    lease = await controls.acquire("route", timeout_seconds=.1)
    assert lease is not None
    for _ in range(3):
        await controls.record_failure("route")
    circuit = controls.circuit_snapshot("route")
    controls.reconfigure({})
    controls.reconfigure({"route": 2})
    assert controls.circuit_snapshot("route") is circuit
    assert circuit.state is CircuitState.OPEN
    second = await controls.acquire("route", timeout_seconds=.1)
    assert second is not None
    assert await controls.acquire("route", timeout_seconds=.01) is None
    lease.release()
    second.release()
    del lease, second
    gc.collect()
    assert controls._limits == {"route": 2}
    assert controls.circuit_snapshot("route") is circuit
