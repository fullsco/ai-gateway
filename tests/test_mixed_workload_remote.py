"""Bounded mixed-lane integration, not a production-throughput or soak claim."""

import asyncio
import json
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import pytest

from gateway.app import create_app
from gateway.config import Settings
from gateway.workflow_context import active_workflow
from gateway.workflow_worker import WorkflowWorker
from tests.mixed_runtime import cleanup_mixed, make_mixed_runtime
from tests.test_remote_isolation import dedicated_pool

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_interactive_streams_progress_while_workflow_holds_shared_provider_capacity():
    async with dedicated_pool() as pool:
        runtime = None
        identities = {}
        started, finish = asyncio.Event(), asyncio.Event()
        ticks = []
        dispatches = {"interactive": 0, "workflow": 0}
        streams = {"opened": 0, "closed": 0}

        class Stream(httpx.AsyncByteStream):
            def __init__(self):
                streams["opened"] += 1
                self.closed = False

            async def __aiter__(self):
                for chunk in (
                    b'data: {"choices":[{"delta":{"content":"ok"},"index":0}]}\n',
                    b'\ndata: {"choices":[],"usage":{"prompt_tokens":4,"completion_tokens":1}}\n\n',
                    b'data: [DONE]\n\n',
                ):
                    await asyncio.sleep(.01)
                    yield chunk

            async def aclose(self):
                if not self.closed:
                    self.closed = True
                    streams["closed"] += 1

        async def provider(request):
            assert request.url.host == "127.0.0.1"
            payload = json.loads(request.content)
            lane = "workflow" if active_workflow.get() else "interactive"
            dispatches[lane] += 1
            if lane == "workflow":
                started.set()
                await finish.wait()
                return httpx.Response(200, json={"id": "synthetic", "object": "chat.completion",
                    "choices": [{"message": {"role": "assistant", "content": "ok"},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 4, "completion_tokens": 1}})
            assert payload["stream"] is True
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  stream=Stream())

        try:
            assert not await pool.fetchval("select exists(select 1 from public.workflow_jobs)")
            runtime, identities = await make_mixed_runtime(pool, provider)
            settings = Settings(_env_file=None, database_url=None, workflows_enabled=True,
                durable_accounting_enabled=True, distributed_admission_enabled=True,
                distributed_provider_capacity=2, distributed_credential_capacity=2,
                service_role="api", health_probe_enabled=False, alert_monitor_enabled=False,
                stream_keepalive_seconds=.1)
            app = create_app(settings, runtime, db_pool=pool)
            worker = WorkflowWorker(pool, runtime, settings)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                        base_url="http://test", timeout=90) as http:
                for size in (8192, 262144):
                    started.clear()
                    finish.clear()
                    job_response = await http.post("/v1/workflows", headers={
                        "Authorization": f"Bearer {identities['workflow'][1]}",
                        "Idempotency-Key": str(uuid4()),
                    }, json={"protocol": "openai_chat_completions", "request": {
                        "model": "qualification-model", "max_tokens": 16, "messages": [],
                    }})
                    assert job_response.status_code == 202
                    job = UUID(job_response.json()["id"])
                    tick = asyncio.create_task(worker.run_once())
                    ticks.append(tick)
                    await asyncio.wait_for(started.wait(), 60)
                    # This request must complete while the workflow's upstream remains blocked.
                    result = await asyncio.wait_for(http.post("/v1/chat/completions", headers={
                        "Authorization": f"Bearer {identities['interactive'][1]}",
                    }, json={"model": "qualification-model", "max_tokens": 16, "stream": True,
                             "messages": [{"role": "user", "content": "x" * size}]}), timeout=90)
                    assert result.status_code == 200, result.text
                    assert b"[DONE]" in result.content
                    assert not tick.done(), "Workflow should still be waiting on its upstream"
                    assert (await http.get("/health")).status_code == 200
                    finish.set()
                    assert await asyncio.wait_for(tick, 60)
                    assert await pool.fetchval(
                        "select status from public.workflow_jobs where id=$1", job,
                    ) == "completed"
            assert dispatches == {"interactive": 2, "workflow": 2}
            assert streams == {"opened": 2, "closed": 2}
            assert runtime.route_controls._active == {}
            assert app.state.drain.active == 0
            for client, _ in identities.values():
                assert await pool.fetchval(
                    "select reserved_cost from public.client_spend_windows where client_id=$1",
                    client,
                ) == Decimal("0.50")
                assert await pool.fetchval("select count(*) from public.financial_reservations "
                    "where client_id=$1 and status='settled'", client) == 2
        finally:
            finish.set()
            for tick in ticks:
                if not tick.done():
                    tick.cancel()
            await asyncio.gather(*ticks, return_exceptions=True)
            if runtime:
                await runtime.http_client.aclose()
            if identities:
                await cleanup_mixed(pool, [client for client, _ in identities.values()])
