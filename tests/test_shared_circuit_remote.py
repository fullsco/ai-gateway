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
        transactions = []
        token = trial_owners.set({})

        class RecordingConnection:
            def __init__(self, connection):
                self.connection = connection

            def __getattr__(self, name):
                return getattr(self.connection, name)

            def transaction(self):
                transactions.append(True)
                return self.connection.transaction()

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
            assert len(statements) == (1 if warm else 4)
            assert len(transactions) == (0 if warm else 1)
            assert statements[0].startswith("select ")
            assert statements[0].endswith("for update")
            if not warm:
                assert statements[1] == statements[0]
                assert statements[2].startswith("insert ")
                assert statements[2].endswith("on conflict do nothing")
                assert statements[3] == statements[0]
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
                self.observed_missing = False

            def __getattr__(self, name):
                return getattr(self.connection, name)

            async def fetchrow(self, query, *args):
                row = await self.connection.fetchrow(query, *args)
                if (row is None and self.connection.is_in_transaction()
                        and not self.observed_missing):
                    self.observed_missing = True
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


@pytest.mark.parametrize("transition", ["probe_claimed", "closed", "deleted"])
async def test_open_circuit_rechecks_state_after_autocommit_lock_is_released(transition):
    async with dedicated_pool() as pool:
        route = f"qualification:{uuid4()}"
        observed = asyncio.Event()
        resume = asyncio.Event()

        class PausedConnection:
            def __init__(self, connection):
                self.connection = connection
                self.paused = False

            def __getattr__(self, name):
                return getattr(self.connection, name)

            async def fetchrow(self, query, *args):
                row = await self.connection.fetchrow(query, *args)
                if not self.paused:
                    self.paused = True
                    assert not self.connection.is_in_transaction()
                    assert row["state"] == "open"
                    observed.set()
                    await resume.wait()
                return row

        async def admit(controls):
            token = trial_owners.set({})
            try:
                allowed = await controls.allow(route)
                return allowed, dict(trial_owners.get())
            finally:
                trial_owners.reset(token)

        try:
            await pool.execute(
                "insert into public.route_circuits(route_id,state,retry_at) "
                "values($1,'open',now()-interval '1 second')", route,
            )
            async with pool.acquire() as first, pool.acquire() as second:
                controls = SharedRouteControls(TransactionPool(PausedConnection(first)),
                                                RouteControls())
                attempt = asyncio.create_task(admit(controls))
                try:
                    async with asyncio.timeout(25):
                        await observed.wait()
                        if transition == "probe_claimed":
                            other = SharedRouteControls(TransactionPool(second), RouteControls())
                            allowed, winner = await admit(other)
                            assert allowed and winner[route][0] is not None
                        elif transition == "closed":
                            await second.execute(
                                "update public.route_circuits set state='closed',generation=1,"
                                "retry_at=null where route_id=$1", route,
                            )
                        else:
                            await second.execute(
                                "delete from public.route_circuits where route_id=$1", route,
                            )
                        resume.set()
                        allowed, owners = await attempt
                        if transition == "probe_claimed":
                            assert not allowed and not owners
                            assert await second.fetchval(
                                "select probe_owner from public.route_circuits where route_id=$1",
                                route,
                            ) == winner[route][0]
                        else:
                            assert allowed
                            assert owners[route] == (None, 1 if transition == "closed" else 0)
                finally:
                    attempt.cancel()
                    await asyncio.gather(attempt, return_exceptions=True)
        finally:
            await pool.execute("delete from public.route_circuits where route_id=$1", route)


async def test_closed_circuit_locking_read_waits_for_concurrent_open_commit():
    async with dedicated_pool() as pool:
        route = f"qualification:{uuid4()}"

        async def admit(connection):
            token = trial_owners.set({})
            try:
                allowed = await SharedRouteControls(TransactionPool(connection),
                                                    RouteControls()).allow(route)
                return allowed, dict(trial_owners.get())
            finally:
                trial_owners.reset(token)

        try:
            await pool.execute("insert into public.route_circuits(route_id) values($1)", route)
            async with pool.acquire() as writer, pool.acquire() as reader:
                task = None
                try:
                    async with asyncio.timeout(25):
                        async with writer.transaction():
                            await writer.execute(
                                "update public.route_circuits set state='open',generation=1,"
                                "retry_at=now()+interval '30 seconds' where route_id=$1", route,
                            )
                            task = asyncio.create_task(admit(reader))
                            # Observe the actual PostgreSQL lock wait, not a scheduling sleep.
                            # A transaction pooler can swap the reader's physical backend;
                            # its connection handshake PID is not an engine lock identity.
                            while not await writer.fetchval(
                                "select exists(select 1 from pg_stat_activity "
                                "where datname=current_database() and pid<>pg_backend_pid() "
                                "and pg_backend_pid()=any(pg_blocking_pids(pid)))",
                            ):
                                if task.done():
                                    await task  # Preserve a dependency error if the query failed.
                                assert not task.done(), "Admission bypassed pending state change"
                                await asyncio.sleep(.02)
                        assert await task == (False, {})
                finally:
                    if task is not None:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
        finally:
            await pool.execute("delete from public.route_circuits where route_id=$1", route)
