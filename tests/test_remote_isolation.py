"""Two-connection tests with committed synthetic data, dedicated remote project only."""

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from gateway.accounting import FinancialLedger
from gateway.configuration import create_pool
from gateway.distributed_capacity import CapacityFull, DistributedCapacity
from gateway.workflow_store import LeaseLost, WorkflowStore
from scripts.dev_db_check import validate_target
from scripts.seed_dbtest import CLIENT, CREDENTIAL, PROVIDER
from tests.test_accounting_integration import TransactionPool

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@asynccontextmanager
async def dedicated_pool():
    dsn = os.environ.get("GATEWAY_DATABASE_URL")
    if not dsn:
        pytest.skip("Dedicated remote qualification database required")
    validate_target(dsn, "kpmxgwflgyzjvitwlfvs")
    pool = await create_pool(dsn)
    try:
        if pool.get_max_size() != 2:
            pytest.fail("Use the dedicated runner --isolated option for two connections")
        yield pool
    finally:
        await pool.close()


@pytest.mark.parametrize("warm", [False, True])
async def test_concurrent_capacity_acquisition_enforces_one_global_slot(warm):
    async with dedicated_pool() as pool:
        scope = f"qualification:{uuid4()}"
        capacity = DistributedCapacity(pool)
        barrier = asyncio.Barrier(2)
        missing_reads = []

        class ContendingConnection:
            def __init__(self, connection):
                self.connection = connection

            def __getattr__(self, name):
                return getattr(self.connection, name)

            async def fetchval(self, query, *args):
                value = await self.connection.fetchval(query, *args)
                if query == (
                    "select capacity from public.capacity_scopes where scope=$1 for update"
                ) and value is None:
                    missing_reads.append(self.connection.get_server_pid())
                    await barrier.wait()
                return value

        async def contender(connection):
            manager = DistributedCapacity(TransactionPool(ContendingConnection(connection)))
            try:
                return await manager.acquire({scope: 1})
            except CapacityFull:
                return None

        try:
            if warm:
                owner = await capacity.acquire({scope: 1})
                await capacity.release(owner)
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
            owners = [owner for owner in results if owner is not None]
            assert len(owners) == 1
            assert len(set(missing_reads)) == (0 if warm else 2)
            assert await pool.fetchval(
                "select count(*) from public.capacity_leases where scope=$1", scope,
            ) == 1
            await capacity.release(owners[0])
            owner = await capacity.acquire({scope: 1})
            await capacity.release(owner)
        finally:
            await pool.execute("delete from public.capacity_leases where scope=$1", scope)
            await pool.execute("delete from public.capacity_scopes where scope=$1", scope)


async def test_separate_processes_observe_the_same_global_capacity():
    async with dedicated_pool() as pool:
        scope = f"qualification:{uuid4()}"
        processes = []
        try:
            # Only two child connections; the parent pool is idle while they contend.
            for _ in range(2):
                processes.append(await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "tests.remote_worker_probe", "capacity", scope,
                    cwd=Path(__file__).resolve().parents[1],
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                ))
            lines = await asyncio.wait_for(asyncio.gather(
                *(process.stdout.readline() for process in processes)
            ), timeout=20)
            assert all(lines), "Capacity probes failed before reporting"
            results = [json.loads(line) for line in lines]
            assert sum(result["admitted"] for result in results) == 1
        finally:
            for process in processes:
                if process.returncode is None:
                    process.kill()
                await asyncio.wait_for(process.wait(), 5)
            await pool.execute("delete from public.capacity_leases where scope=$1", scope)
            await pool.execute("delete from public.capacity_scopes where scope=$1", scope)


async def test_authoritative_capacity_decrease_applies_to_stale_replica_configuration():
    async with dedicated_pool() as pool:
        scope = f"qualification:{uuid4()}"
        capacity = DistributedCapacity(pool)
        owners = []
        try:
            owners.append(await capacity.acquire({scope: 2}))
            policy = await capacity.configure(scope, 1, expected_version=0)
            assert policy["version"] == 1
            with pytest.raises(CapacityFull):
                await capacity.acquire({scope: 2})  # old replica still thinks cap is two
            from gateway.distributed_capacity import CapacityUnavailable

            with pytest.raises(CapacityUnavailable):
                await capacity.configure(scope, 3, expected_version=0)
            await capacity.configure(scope, 2, expected_version=1)
            owners.append(await capacity.acquire({scope: 2}))
        finally:
            for owner in owners:
                await capacity.release(owner)
            await pool.execute("delete from public.capacity_scopes where scope=$1", scope)


async def test_skip_locked_claim_and_committed_event_visibility():
    async with dedicated_pool() as pool:
        store = WorkflowStore(pool, per_client_capacity=2)
        if await pool.fetchval("select exists(select 1 from public.workflow_jobs)"):
            pytest.fail("Dedicated job table must be empty before cross-connection claim test")
        jobs = []
        try:
            for _ in range(2):
                jobs.append(await store.submit(CLIENT, str(uuid4()), {"synthetic": True}))
            async with pool.acquire() as locked, locked.transaction():
                await locked.fetchval(
                    "select id from public.workflow_jobs where id=$1 for update", jobs[0]["id"],
                )
                claimed = await asyncio.wait_for(store.claim(uuid4()), timeout=15)
                assert claimed["id"] == jobs[1]["id"]
                # The second connection committed both the transition and its event.
                assert await locked.fetchval(
                    "select status from public.workflow_jobs where id=$1", claimed["id"],
                ) == "running"
                assert await locked.fetchval(
                    "select kind from public.workflow_events where job_id=$1 and sequence=1",
                    claimed["id"],
                ) == "running"
            claimed = await store.claim(uuid4())
            assert claimed["id"] == jobs[0]["id"]
            assert await store.claim(uuid4()) is None
        finally:
            for job in jobs:
                await pool.execute("delete from public.workflow_jobs where id=$1", job["id"])


async def test_connection_loss_rolls_back_uncommitted_transition_and_event():
    async with dedicated_pool() as pool:
        store = WorkflowStore(pool)
        job = await store.submit(CLIENT, str(uuid4()), {"synthetic": True})
        connection = await pool.acquire()
        try:
            transaction = connection.transaction()
            await transaction.start()
            await connection.execute(
                "update public.workflow_jobs set status='running',version=1 where id=$1", job["id"],
            )
            await connection.execute(
                "insert into public.workflow_events(job_id,sequence,kind) values($1,1,'running')",
                job["id"],
            )
            connection.terminate()
            # Synchronize behind the abandoned row lock on a second backend; no sleeps.
            async with pool.acquire() as observer, observer.transaction():
                await observer.execute("set local lock_timeout='5s'")
                row = await observer.fetchrow(
                    "select status,version from public.workflow_jobs where id=$1 for update",
                    job["id"],
                )
                assert dict(row) == {"status": "queued", "version": 0}
                assert await observer.fetchval(
                    "select count(*) from public.workflow_events where job_id=$1", job["id"],
                ) == 1
        finally:
            await pool.release(connection)
            await pool.execute("delete from public.workflow_jobs where id=$1", job["id"])


async def test_renewal_blocked_on_row_lock_cannot_revive_expired_lease():
    async with dedicated_pool() as pool:
        scope = f"qualification:{uuid4()}"
        capacity = DistributedCapacity(pool, ttl=3)
        owner = await capacity.acquire({scope: 1})
        renewal = None
        try:
            async with pool.acquire() as blocker, blocker.transaction():
                await blocker.execute(
                    "update public.capacity_leases set expires_at=clock_timestamp() "
                    "+interval '1 second' where owner=$1", owner,
                )
                renewal = asyncio.create_task(capacity.renew(owner, 1))
                await asyncio.sleep(1.3)
            from gateway.distributed_capacity import CapacityUnavailable

            with pytest.raises(CapacityUnavailable):
                await asyncio.wait_for(renewal, timeout=10)
        finally:
            if renewal is not None and not renewal.done():
                renewal.cancel()
                await asyncio.gather(renewal, return_exceptions=True)
            await capacity.release(owner)
            await pool.execute("delete from public.capacity_scopes where scope=$1", scope)


async def test_process_death_after_settlement_intent_is_replayed_once_by_competing_workers():
    async with dedicated_pool() as pool:
        ledger = FinancialLedger(pool)
        client = uuid4()
        reservation = None
        process = None
        try:
            await pool.execute(
                "insert into public.gateway_clients(id,name,spending_limit) "
                "values($1,'Synthetic settlement crash',10)", client,
            )
            route = SimpleNamespace(
                canonical_model_id="qualification-model",
                credential=SimpleNamespace(credential_id=CREDENTIAL),
                provider_model=SimpleNamespace(provider_id=PROVIDER, route_id=None),
            )
            reservation = await ledger.reserve(request_id=str(uuid4()), attempt_number=1,
                client_id=client, route=route, currency="USD", estimated_cost=Decimal(4))
            await ledger.dispatch(reservation)
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "tests.remote_worker_probe", "settlement", str(reservation),
                cwd=Path(__file__).resolve().parents[1],
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            line = await asyncio.wait_for(process.stdout.readline(), timeout=25)
            assert line and json.loads(line)["phase"] == "settlement_pending"
            process.kill()
            await asyncio.wait_for(process.wait(), timeout=5)
            assert await pool.fetchval(
                "select status from public.financial_reservations where id=$1", reservation,
            ) == "settlement_pending"
            await asyncio.wait_for(
                asyncio.gather(ledger.apply(reservation), ledger.apply(reservation)), timeout=20,
            )
            assert await pool.fetchval(
                "select reserved_cost from public.client_spend_windows where client_id=$1", client,
            ) == Decimal(1)
            assert await pool.fetchval(
                "select status from public.financial_reservations where id=$1", reservation,
            ) == "settled"
            with pytest.raises(ValueError, match="Conflicting"):
                await ledger.settle(reservation, actual=Decimal(2))
        finally:
            if process is not None and process.returncode is None:
                process.kill()
                await asyncio.wait_for(process.wait(), timeout=5)
            if reservation is not None:
                await pool.execute("delete from public.settlement_outbox where reservation_id=$1",
                                   reservation)
                await pool.execute("delete from public.financial_reservations where id=$1",
                                   reservation)
            await pool.execute("delete from public.gateway_clients where id=$1", client)


async def test_concurrent_client_reservations_do_not_deadlock_or_exceed_budget():
    async with dedicated_pool() as pool:
        client = uuid4()
        ledger = FinancialLedger(pool)
        try:
            await pool.execute("insert into public.gateway_clients(id,name,spending_limit) "
                               "values($1,'Synthetic concurrent budget',5)", client)
            route = SimpleNamespace(canonical_model_id="qualification-model",
                credential=SimpleNamespace(credential_id=CREDENTIAL),
                provider_model=SimpleNamespace(provider_id=PROVIDER, route_id=None))

            async def reserve():
                return await ledger.reserve(request_id=str(uuid4()), attempt_number=1,
                    client_id=client, route=route, currency="USD", estimated_cost=Decimal(4))

            from gateway.quotas import BudgetExceeded

            results = await asyncio.wait_for(asyncio.gather(reserve(), reserve(),
                                             return_exceptions=True), timeout=20)
            assert sum(isinstance(result, BudgetExceeded) for result in results) == 1
            assert sum(isinstance(result, UUID) for result in results) == 1
            assert await pool.fetchval(
                "select reserved_cost from public.client_spend_windows where client_id=$1", client,
            ) == 4
        finally:
            await pool.execute("delete from public.financial_reservations where client_id=$1",
                               client)
            await pool.execute("delete from public.gateway_clients where id=$1", client)


@pytest.mark.parametrize("phase", ["claimed", "completed"])
async def test_worker_process_death_preserves_committed_state_and_fences_old_owner(phase):
    async with dedicated_pool() as pool:
        store = WorkflowStore(pool)
        if await pool.fetchval("select exists(select 1 from public.workflow_jobs)"):
            pytest.fail("Dedicated job table must be empty for worker process test")
        job = await store.submit(CLIENT, str(uuid4()), {"synthetic": True})
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "tests.remote_worker_probe", phase,
                cwd=Path(__file__).resolve().parents[1],
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            line = await asyncio.wait_for(process.stdout.readline(), timeout=25)
            assert line, "Worker did not reach the expected crash point"
            evidence = json.loads(line)
            assert evidence["job_id"] == str(job["id"])
            process.kill()
            await asyncio.wait_for(process.wait(), timeout=5)
            assert process.returncode != 0
            persisted = await store.get(CLIENT, job["id"])
            if phase == "completed":
                assert persisted["status"] == "completed"
                assert persisted["result"] == {"synthetic": "durable-result"}
                assert await store.recover() == 0
            else:
                assert persisted["status"] == "running"
                # Advance only this synthetic lease; the process really died, expiry is accelerated.
                await pool.execute(
                    "update public.workflow_jobs set lease_expires_at=now()-interval '1 second' "
                    "where id=$1", job["id"],
                )
                assert await store.recover() == 1
                assert (await store.get(CLIENT, job["id"]))["status"] == "waiting_for_approval"
            assert await store.claim(uuid4()) is None
            with pytest.raises(LeaseLost):
                await store.transition(job["id"], UUID(evidence["owner"]), evidence["version"],
                                       "completed", {}, {"stale": True})
        finally:
            if process is not None and process.returncode is None:
                process.kill()
                await asyncio.wait_for(process.wait(), timeout=5)
            await pool.execute("delete from public.workflow_jobs where id=$1", job["id"])


@pytest.mark.parametrize("failure", ["unreachable", "stalled"])
async def test_capacity_renewal_failure_cancels_owner_and_reclaims_only_expired_lease(failure):
    async with dedicated_pool() as pool:
        scope = f"qualification:{uuid4()}"
        capacity = DistributedCapacity(pool, ttl=3)
        owner = await capacity.acquire({scope: 1})
        cancelled = asyncio.Event()

        async def work():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async def failed_renew(*_):
            if failure == "unreachable":
                raise ConnectionError("synthetic renewal connection failure")
            await asyncio.Event().wait()

        # Inject at the DB boundary; no firewall or shared network/service changes.
        capacity.renew = failed_renew
        task = asyncio.create_task(work())
        pulse = capacity.guard(owner, 1, task)
        replacement = None
        try:
            await asyncio.wait_for(cancelled.wait(), timeout=5)
            with pytest.raises(asyncio.CancelledError):
                await task
            # Expiry is advanced on our own row; a replacement must survive stale release.
            await pool.execute(
                "update public.capacity_leases set expires_at=now()-interval '1 second' "
                "where owner=$1", owner,
            )
            replacement = await capacity.acquire({scope: 1})
            await capacity.release(owner)
            assert await pool.fetchval(
                "select count(*) from public.capacity_leases where owner=$1", replacement,
            ) == 1
        finally:
            task.cancel()
            pulse.cancel()
            await asyncio.gather(task, pulse, return_exceptions=True)
            await capacity.release(owner)
            if replacement:
                await capacity.release(replacement)
            await pool.execute("delete from public.capacity_scopes where scope=$1", scope)
