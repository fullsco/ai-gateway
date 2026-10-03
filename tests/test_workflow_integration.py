from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from gateway.accounting import FinancialLedger
from gateway.quotas import QuotaUnavailable
from gateway.workflow_context import active_workflow
from gateway.workflow_store import LeaseLost, WorkflowConflict, WorkflowStore
from tests.test_accounting_integration import TransactionPool, fixture

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def store_for(db):
    if not await db.fetchval("select to_regclass('public.workflow_jobs')"):
        migration = Path(__file__).parents[1] / "migrations/027_workflow_jobs.sql"
        await db.execute(migration.read_text())
        bounds = Path(__file__).parents[1] / "migrations/029_workflow_execution_bounds.sql"
        await db.execute(bounds.read_text())
    return WorkflowStore(TransactionPool(db))


async def test_workflow_idempotency_tenant_boundary_and_cancel_fencing():
    async with fixture() as (_, db, client, _):
        store = await store_for(db)
        job = await store.submit(client, "synthetic", {"request": {"model": "test"}})
        replay = await store.submit(client, "synthetic", {"request": {"model": "test"}})
        assert replay["id"] == job["id"]
        with pytest.raises(WorkflowConflict):
            await store.submit(client, "synthetic", {"request": {"model": "different"}})
        assert await store.get(uuid4(), job["id"]) is None
        assert await store.events(uuid4(), job["id"], -1) == []
        owner = uuid4()
        claimed = await store.claim(owner)
        assert claimed["id"] == job["id"]
        cancelled = await store.cancel(client, job["id"])
        assert cancelled["status"] == "cancelled"
        with pytest.raises(LeaseLost):
            await store.transition(job["id"], owner, claimed["version"], "completed", {}, {})
        assert [event["kind"] for event in await store.events(client, job["id"], -1)] == [
            "queued", "running", "cancelled",
        ]


async def test_tool_wait_result_deduplication_and_checkpoint_resume():
    async with fixture() as (_, db, client, _):
        store = await store_for(db)
        job = await store.submit(client, "synthetic", {})
        owner = uuid4()
        claimed = await store.claim(owner)
        await store.transition(job["id"], owner, claimed["version"], "waiting_for_tool",
                               {"invocation_id": "tool-1"})
        resumed = await store.tool_result(client, job["id"], "tool-1", {"answer": 42})
        assert resumed["status"] == "queued"
        assert resumed["checkpoint"]["tool_result"] == {"answer": 42}
        replay = await store.tool_result(client, job["id"], "tool-1", {"answer": 42})
        assert replay["version"] == resumed["version"]
        with pytest.raises(WorkflowConflict):
            await store.tool_result(client, job["id"], "tool-1", {"answer": 43})


async def test_lost_lease_requires_explicit_reconciliation_and_stale_owner_cannot_commit():
    async with fixture() as (_, db, client, _):
        store = await store_for(db)
        job = await store.submit(client, "synthetic", {})
        owner = uuid4()
        claimed = await store.claim(owner)
        await db.execute("update public.workflow_jobs "
                         "set lease_expires_at=now()-interval '1 second' "
                         "where id=$1", job["id"])
        assert await store.recover() == 1
        paused = await store.get(client, job["id"])
        assert paused["status"] == "waiting_for_approval"
        with pytest.raises(LeaseLost):
            await store.renew(job["id"], owner, claimed["version"])
        resumed = await store.resume(client, job["id"], paused["version"])
        assert resumed["status"] == "queued"
        with pytest.raises(WorkflowConflict):
            await store.resume(client, job["id"], paused["version"])


async def test_workflow_cancel_between_reservation_and_dispatch_blocks_provider_work():
    async with fixture() as (_, db, client, route):
        store = await store_for(db)
        job = await store.submit(client, "synthetic", {"max_cost": "10"})
        owner = uuid4()
        claimed = await store.claim(owner)
        ledger = FinancialLedger(TransactionPool(db))
        token = active_workflow.set((job["id"], owner, claimed["version"]))
        try:
            reservation = await ledger.reserve(request_id=str(uuid4()), attempt_number=1,
                client_id=client, route=route, currency="USD", estimated_cost=Decimal(1),
                estimated_tokens=100)
            await store.cancel(client, job["id"])
            with pytest.raises(QuotaUnavailable, match="lost ownership"):
                await ledger.dispatch(reservation)
            assert await db.fetchval(
                "select status from public.financial_reservations where id=$1", reservation,
            ) == "reserved"
            await ledger.settle(reservation, actual=Decimal(0))
            assert await db.fetchval("select reserved_tokens from public.workflow_jobs where id=$1",
                                     job["id"]) == 0
        finally:
            active_workflow.reset(token)


async def test_tenant_pending_limit_and_running_limit_are_enforced():
    async with fixture() as (_, db, client, _):
        await store_for(db)
        store = WorkflowStore(TransactionPool(db), per_client_capacity=1, max_pending=2)
        first = await store.submit(client, "first", {})
        await store.submit(client, "second", {})
        assert (await store.submit(client, "first", {}))["id"] == first["id"]
        with pytest.raises(WorkflowConflict, match="capacity"):
            await store.submit(client, "third", {})
        claimed = await store.claim(uuid4())
        assert claimed is not None
        assert await store.claim(uuid4()) is None
        await store.cancel(client, claimed["id"])
        assert await store.claim(uuid4()) is not None
