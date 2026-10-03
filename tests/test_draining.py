import asyncio

import httpx
import pytest

from gateway.admission import InferenceAdmissionMiddleware
from gateway.config import Settings
from gateway.draining import DrainState
from tests.test_admission import Exchange, echo


@pytest.mark.asyncio
async def test_drain_rejects_new_requests_and_waits_for_active_cleanup():
    finish = asyncio.Event()
    started = asyncio.Event()

    async def handler(scope, receive, send):
        await echo(scope, receive, send)
        started.set()
        await finish.wait()

    state = DrainState()
    app = InferenceAdmissionMiddleware(handler, Settings(_env_file=None), drain=state)
    active = asyncio.create_task(Exchange("/v1/messages").run(app))
    try:
        await asyncio.wait_for(started.wait(), 1)
        assert not await state.drain(.01)
        rejected = Exchange("/v1/messages")
        await rejected.run(app)
        assert rejected.status == 503
        assert rejected.receive_calls == 0
        assert state.active == 1
        finish.set()
        await active
        assert await state.drain(1)
        assert state.active == 0
    finally:
        active.cancel()
        await asyncio.gather(active, return_exceptions=True)


@pytest.mark.asyncio
async def test_drain_endpoint_requires_admin_and_readiness_tracks_drain():
    from gateway.app import create_app

    app = create_app(Settings(_env_file=None, database_url=None))
    app.state.ready = True
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                base_url="http://test") as client:
        assert (await client.post("/api/admin/v1/drain")).status_code == 401
        assert (await client.get("/ready")).status_code == 200
        await app.state.drain.drain(1)
        assert (await client.get("/ready")).status_code == 503
        assert (await client.get("/health")).status_code == 200
