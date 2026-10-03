from types import SimpleNamespace
from uuid import uuid4

import pytest

from gateway.distributed_capacity import (
    CapacityFull,
    DistributedCapacity,
    capacity_lifetime,
    reserve_route_capacity,
)
from gateway.workflow_context import active_workflow
from tests.test_remote_isolation import dedicated_pool

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_saturated_workflow_share_leaves_an_interactive_slot_available():
    async with dedicated_pool() as pool:
        identifier = f"qualification-{uuid4()}"
        route = SimpleNamespace(
            provider_model=SimpleNamespace(
                provider_id=identifier, id=identifier, max_concurrency=2,
            ),
            credential=SimpleNamespace(credential_id=identifier),
        )
        settings = SimpleNamespace(
            distributed_provider_capacity=2, distributed_credential_capacity=2,
        )
        scopes = {f"provider:{identifier}": 2, f"credential:{identifier}": 2,
                  f"route:{identifier}": 2}
        capacity = DistributedCapacity(pool)
        interactive = None
        token = active_workflow.set((uuid4(), uuid4(), 1))
        try:
            async with capacity_lifetime():
                await reserve_route_capacity(pool, route, settings)
                async with capacity_lifetime():
                    with pytest.raises(CapacityFull):
                        await reserve_route_capacity(pool, route, settings)
                # Interactive admission does not consume the workflow-only share.
                interactive = await capacity.acquire(scopes)
                await capacity.release(interactive)
                interactive = None
        finally:
            active_workflow.reset(token)
            if interactive:
                await capacity.release(interactive)
            all_scopes = [prefix + scope for prefix in ("", "workflow-share:") for scope in scopes]
            await pool.execute("delete from public.capacity_leases where scope=any($1::text[])",
                               all_scopes)
            await pool.execute("delete from public.capacity_scopes where scope=any($1::text[])",
                               all_scopes)


async def test_persisted_capacity_decrease_reduces_background_share_for_old_workers():
    async with dedicated_pool() as pool:
        identifier = f"qualification-{uuid4()}"
        parent, share = f"provider:{identifier}", f"workflow-share:provider:{identifier}"
        capacity = DistributedCapacity(pool)
        first = second = None
        try:
            initial = await capacity.acquire({parent: 32, share: 16})
            await capacity.release(initial)
            await capacity.configure(parent, 2, expected_version=0)
            first = await capacity.acquire({parent: 32, share: 16})
            with pytest.raises(CapacityFull):
                await capacity.acquire({parent: 32, share: 16})
            second = await capacity.acquire({parent: 32})
        finally:
            for owner in (first, second):
                if owner:
                    await capacity.release(owner)
            await pool.execute("delete from public.capacity_leases where scope=any($1::text[])",
                               [parent, share])
            await pool.execute("delete from public.capacity_scopes where scope=any($1::text[])",
                               [parent, share])
