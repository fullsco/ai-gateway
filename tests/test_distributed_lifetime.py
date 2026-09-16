import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.distributed_capacity import capacity_lifetime, reserve_route_capacity


@pytest.mark.asyncio
async def test_retry_releases_previous_route_ownership_before_acquiring_sibling(monkeypatch):
    live = set()
    events = []

    class Capacity:
        def __init__(self, pool):
            pass

        async def acquire(self, scopes):
            assert not live, "Retry must not compete with its own provider slot"
            owner = len(events)
            live.add(owner)
            events.append("acquire")
            return owner

        async def release(self, owner):
            live.remove(owner)
            events.append("release")

        def guard(self, owner, count):
            return asyncio.create_task(asyncio.sleep(100))

    monkeypatch.setattr("gateway.distributed_capacity.DistributedCapacity", Capacity)
    settings = SimpleNamespace(distributed_provider_capacity=1, distributed_credential_capacity=1)
    mapping = SimpleNamespace(provider_id="provider", id="route", max_concurrency=1)
    async with capacity_lifetime():
        for credential in ("a", "b"):
            route = SimpleNamespace(provider_model=mapping,
                                    credential=SimpleNamespace(credential_id=credential))
            await reserve_route_capacity(AsyncMock(), route, settings)
    assert events == ["acquire", "release", "acquire", "release"]
    assert not live
