from pathlib import Path
from uuid import uuid4

import pytest

from gateway.distributed_capacity import CapacityFull, CapacityUnavailable, DistributedCapacity
from tests.test_accounting_integration import TransactionPool, fixture

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_capacity_is_atomic_across_scopes_and_expired_owner_cannot_renew():
    async with fixture() as (_, db, _, _):
        if not await db.fetchval("select to_regclass('public.capacity_scopes')"):
            migration = Path(__file__).parents[1] / "migrations/028_capacity_leases.sql"
            await db.execute(migration.read_text())
        capacity = DistributedCapacity(TransactionPool(db))
        prefix = f"synthetic-{uuid4()}"
        scopes = {f"{prefix}:global": 2, f"{prefix}:client": 1}
        first = await capacity.acquire(scopes)
        with pytest.raises(CapacityFull):
            await capacity.acquire(scopes)
        assert await db.fetchval(
            "select count(*) from public.capacity_leases where scope=any($1::text[])", list(scopes)
        ) == 2
        await capacity.renew(first, 2)
        await db.execute("update public.capacity_leases "
                         "set expires_at=now()-interval '1 second' where owner=$1", first)
        with pytest.raises(CapacityUnavailable):
            await capacity.renew(first, 2)
        second = await capacity.acquire(scopes)
        await capacity.release(first)
        count = await db.fetchval(
            "select count(*) from public.capacity_leases where owner=$1", second
        )
        assert count == 2
        await capacity.release(second)


async def test_batched_capacity_shares_respect_parent_policy_and_start_together():
    async with fixture() as (_, db, _, _):
        capacity = DistributedCapacity(TransactionPool(db))
        prefix = f"provider:synthetic-{uuid4()}"
        share = f"workflow-share:{prefix}"
        # Stored parent cap wins over a replica's higher local default.
        await db.execute("insert into public.capacity_scopes(scope,capacity) values($1,2)", prefix)
        scopes = {share: 4, prefix: 8}
        first = await capacity.acquire(scopes)
        assert await db.fetchval(
            "select count(distinct expires_at) from public.capacity_leases where owner=$1", first,
        ) == 1
        with pytest.raises(CapacityFull):
            await capacity.acquire(scopes)
        # A rejected batch must not consume the parent's spare interactive slot.
        second = await capacity.acquire({prefix: 8})
        with pytest.raises(CapacityFull):
            await capacity.acquire({prefix: 8})
        await capacity.release(first)
        await capacity.release(second)
