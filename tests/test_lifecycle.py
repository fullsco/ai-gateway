import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.lifecycle import LifecycleResources, managed_lifespan


@pytest.mark.asyncio
async def test_failed_startup_closes_resources_and_cancels_started_tasks():
    resource = SimpleNamespace(close=AsyncMock())
    app = SimpleNamespace(state=SimpleNamespace(
        settings=SimpleNamespace(shutdown_timeout_seconds=1), ready=True,
    ))
    tasks = []

    @managed_lifespan
    async def start(app):
        resources = app.state.lifecycle_resources
        resources.resource(resource)
        tasks.append(resources.task(asyncio.sleep(100)))
        raise RuntimeError("synthetic initialization failure")
        yield  # noqa: B018 - makes the intentionally failing startup an async generator

    with pytest.raises(RuntimeError, match="initialization failure"):
        async with start(app):
            pytest.fail("Startup unexpectedly succeeded")
    resource.close.assert_awaited_once()
    assert tasks[0].cancelled()
    assert app.state.ready is False


@pytest.mark.asyncio
async def test_shutdown_deadline_terminates_pool_instead_of_waiting_forever():
    async def hang():
        await asyncio.Event().wait()

    resource = SimpleNamespace(close=hang, terminate=Mock())
    resources = LifecycleResources(.02)
    resources.resource(resource)
    await asyncio.wait_for(resources.close(), timeout=1)
    resource.terminate.assert_called_once()
