import asyncio
from types import SimpleNamespace

import pytest

from gateway.draining import DrainState
from gateway.worker_service import workflow_loop


@pytest.mark.asyncio
async def test_worker_drain_waits_for_current_tick_and_stops_claiming(monkeypatch):
    entered, finish = asyncio.Event(), asyncio.Event()
    releasing, release_done = asyncio.Event(), asyncio.Event()
    calls = []

    class Capacity:
        def __init__(self, pool):
            pass

        async def acquire(self, scopes):
            return "synthetic"

        async def release(self, owner):
            releasing.set()
            await release_done.wait()

        def guard(self, owner, count, task):
            return asyncio.create_task(asyncio.sleep(100))

    class Worker:
        def __init__(self, pool, runtime, settings, **kwargs):
            assert kwargs["live_state"] == "live"
            assert kwargs["health_recorder"] == "health"

        async def run_once(self):
            calls.append("claim")
            entered.set()
            await finish.wait()
            return True

    monkeypatch.setattr("gateway.worker_service.DistributedCapacity", Capacity)
    monkeypatch.setattr("gateway.worker_service.WorkflowWorker", Worker)
    drain = DrainState()
    app = SimpleNamespace(state=SimpleNamespace(
        runtime=object(), db_pool=None, drain=drain,
        settings=SimpleNamespace(workflow_global_capacity=1), live_state="live",
        health_recorder="health",
    ))
    task = asyncio.create_task(workflow_loop(app))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert not await drain.drain(.01)
        assert drain.active == 1
        finish.set()
        await asyncio.wait_for(releasing.wait(), 1)
        assert drain.active == 1
        assert not await drain.drain(.01)
        release_done.set()
        await asyncio.wait_for(task, 1)
        assert drain.active == 0
        assert calls == ["claim"]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
