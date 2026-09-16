import asyncio
from uuid import uuid4

import pytest

from gateway.distributed_capacity import capacity_lifetime
from gateway.routing import RouteControls
from gateway.shared_circuit import SharedRouteControls, trial_owners
from tests.test_remote_isolation import dedicated_pool

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_shared_circuit_serializes_half_open_trials_and_retains_failure_state():
    async with dedicated_pool() as pool:
        route = f"qualification:{uuid4()}"
        controls = SharedRouteControls(pool, RouteControls())
        token = trial_owners.set({})
        try:
            for _ in range(3):
                assert await controls.allow(route)
                await controls.record_failure(route)
            assert not await controls.allow(route)
            await pool.execute("update public.route_circuits "
                               "set retry_at=now()-interval '1 second' "
                               "where route_id=$1", route)

            async def trial():
                token = trial_owners.set({})
                try:
                    admitted = await controls.allow(route)
                    return admitted, dict(trial_owners.get())
                finally:
                    trial_owners.reset(token)

            attempts = await asyncio.gather(trial(), trial())
            assert sum(admitted for admitted, _ in attempts) == 1
            owners = next(owners for admitted, owners in attempts if admitted)
            trial_owners.set(owners)
            await controls.record_success(route)
            assert await controls.allow(route)
            await controls.record_success(route)
            assert await pool.fetchval("select state from public.route_circuits where route_id=$1",
                                       route) == "closed"
        finally:
            trial_owners.reset(token)
            await pool.execute("delete from public.route_circuits where route_id=$1", route)


async def test_late_success_from_prior_generation_cannot_close_newly_open_circuit():
    async with dedicated_pool() as pool:
        route = f"qualification:{uuid4()}"
        controls = SharedRouteControls(pool, RouteControls())
        token = trial_owners.set({})
        try:
            assert await controls.allow(route)
            stale = dict(trial_owners.get())
            for _ in range(3):
                assert await controls.allow(route)
                await controls.record_failure(route)
            for _ in range(2):
                trial_owners.set(dict(stale))
                await controls.record_success(route)
            assert not await controls.allow(route)
            assert await pool.fetchval(
                "select state from public.route_circuits where route_id=$1", route,
            ) == "open"
        finally:
            trial_owners.reset(token)
            await pool.execute("delete from public.route_circuits where route_id=$1", route)


async def test_probe_lifetime_retains_owner_and_releases_on_cancelled_attempt():
    async with dedicated_pool() as pool:
        route = f"qualification:{uuid4()}"
        controls = SharedRouteControls(pool, RouteControls())
        token = trial_owners.set({})
        try:
            for _ in range(3):
                assert await controls.allow(route)
                await controls.record_failure(route)
            await pool.execute("update public.route_circuits set retry_at=clock_timestamp() "
                               "where route_id=$1", route)
            async with capacity_lifetime():
                assert await controls.allow(route)
                assert len(controls.pulses) == 1
                assert not await controls.allow(route)
                owner = await pool.fetchval("select probe_owner from public.route_circuits "
                                            "where route_id=$1", route)
                assert owner is not None
            assert await pool.fetchval("select probe_owner from public.route_circuits "
                                       "where route_id=$1", route) is None
            assert all(pulse.done() for pulse in controls.pulses.values())
        finally:
            trial_owners.reset(token)
            await pool.execute("delete from public.route_circuits where route_id=$1", route)
