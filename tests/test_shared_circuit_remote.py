import asyncio
from uuid import uuid4

import pytest

from gateway.distributed_capacity import capacity_lifetime
from gateway.routing import RouteControls
from gateway.shared_circuit import SharedRouteControls, trial_owners
from tests.test_accounting_integration import TransactionPool
from tests.test_remote_isolation import dedicated_pool

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.mark.parametrize("warm", [False, True])
async def test_shared_circuit_admission_statement_counts(warm):
    async with dedicated_pool() as pool:
        route = f"qualification:{uuid4()}"
        statements = []
        token = trial_owners.set({})

        class RecordingConnection:
            def __init__(self, connection):
                self.connection = connection

            def __getattr__(self, name):
                return getattr(self.connection, name)

            async def execute(self, query, *args):
                statements.append(query)
                return await self.connection.execute(query, *args)

            async def fetchrow(self, query, *args):
                statements.append(query)
                return await self.connection.fetchrow(query, *args)

        try:
            if warm:
                await pool.execute(
                    "insert into public.route_circuits(route_id) values($1)", route,
                )
            async with pool.acquire() as connection:
                controls = SharedRouteControls(
                    TransactionPool(RecordingConnection(connection)), RouteControls(),
                )
                assert await controls.allow(route)
            assert trial_owners.get()[route] == (None, 0)
            assert len(statements) == (1 if warm else 3)
            assert statements[0].startswith("select ")
            assert statements[0].endswith("for update")
            if not warm:
                assert statements[1].startswith("insert ")
                assert statements[1].endswith("on conflict do nothing")
                assert statements[2] == statements[0]
        finally:
            trial_owners.reset(token)
            await pool.execute("delete from public.route_circuits where route_id=$1", route)


async def test_shared_circuit_concurrent_cold_creation_preserves_one_row():
    async with dedicated_pool() as pool:
        route = f"qualification:{uuid4()}"
        barrier = asyncio.Barrier(2)
        missing_reads = []

        class ContendingConnection:
            def __init__(self, connection):
                self.connection = connection

            def __getattr__(self, name):
                return getattr(self.connection, name)

            async def fetchrow(self, query, *args):
                row = await self.connection.fetchrow(query, *args)
                if row is None:
                    missing_reads.append(self.connection.get_server_pid())
                    await barrier.wait()
                return row

        async def contender(connection):
            token = trial_owners.set({})
            try:
                controls = SharedRouteControls(
                    TransactionPool(ContendingConnection(connection)), RouteControls(),
                )
                admitted = await controls.allow(route)
                return admitted, trial_owners.get()[route]
            finally:
                trial_owners.reset(token)

        try:
            async with pool.acquire() as first, pool.acquire() as second:
                assert first.get_server_pid() != second.get_server_pid()
                tasks = [asyncio.create_task(contender(connection))
                         for connection in (first, second)]
                try:
                    results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=30)
                finally:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
            assert results == [(True, (None, 0)), (True, (None, 0))]
            assert len(missing_reads) == len(set(missing_reads)) == 2
            rows = await pool.fetch(
                "select state,generation,probe_owner from public.route_circuits where route_id=$1",
                route,
            )
            assert len(rows) == 1
            assert dict(rows[0]) == {"state": "closed", "generation": 0, "probe_owner": None}
        finally:
            await pool.execute("delete from public.route_circuits where route_id=$1", route)


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
