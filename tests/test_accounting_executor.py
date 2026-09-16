import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
import pytest

from gateway.api.executor import execute_request
from gateway.config import Settings
from gateway.protocols import ClientProtocol, normalize_request
from gateway.routing import RouteControls
from tests.test_messages_api import BytesStream, make_runtime


@pytest.mark.asyncio
async def test_settlement_failure_never_retries_a_completed_upstream(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"type": "message", "content": [],
                                        "usage": {"input_tokens": 1, "output_tokens": 1}})

    runtime, _ = make_runtime(handler)
    ledger = AsyncMock()
    ledger.reserve.return_value = "synthetic-reservation"
    ledger.settle.side_effect = RuntimeError("database unavailable")
    monkeypatch.setattr("gateway.api.executor.FinancialLedger", lambda _: ledger)
    pool = AsyncMock()
    pool.fetchval.return_value = 1
    try:
        result = await execute_request(
            normalize_request(ClientProtocol.ANTHROPIC_MESSAGES,
                              {"model": "model-x", "max_tokens": 10, "messages": []}),
            {}, runtime, Settings(_env_file=None, durable_accounting_enabled=True),
            client_id="client-1", db_pool=pool,
        )
        assert result.status_code == 503
        assert len(calls) == 1
        ledger.dispatch.assert_awaited_once()
        ledger.settle.assert_awaited_once()
    finally:
        await runtime.http_client.aclose()


@pytest.mark.asyncio
async def test_missing_durable_database_rejects_before_provider_dispatch():
    calls = []
    runtime, _ = make_runtime(lambda request: calls.append(request))
    try:
        result = await execute_request(
            normalize_request(ClientProtocol.ANTHROPIC_MESSAGES, {"model": "model-x"}),
            {}, runtime, Settings(_env_file=None, durable_accounting_enabled=True),
            client_id="client-1",
        )
        assert result.status_code == 503
        assert calls == []
    finally:
        await runtime.http_client.aclose()


@pytest.mark.asyncio
async def test_cancellation_during_attempt_logging_releases_concurrency_and_probe(monkeypatch):
    calls = []
    runtime, _ = make_runtime(lambda request: calls.append(request))
    controls = RouteControls({"provider-model-x": 1})
    controls.abandon = AsyncMock(wraps=controls.abandon)
    runtime = replace(runtime, route_controls=controls)
    reached = asyncio.Event()

    async def blocked(*args, **kwargs):
        reached.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("gateway.api.executor.RequestRecorder.start_attempt", blocked)
    task = asyncio.create_task(execute_request(
        normalize_request(ClientProtocol.ANTHROPIC_MESSAGES, {"model": "model-x"}),
        {}, runtime, Settings(_env_file=None), client_id="client-1",
    ))
    try:
        await asyncio.wait_for(reached.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        recovered = await controls.acquire("provider-model-x", timeout_seconds=.1)
        assert recovered is not None
        recovered.release()
        controls.abandon.assert_awaited_once()
        assert not calls
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.http_client.aclose()


@pytest.mark.asyncio
async def test_clean_eof_without_anthropic_terminal_retains_unknown_usage_hold(monkeypatch):
    runtime, _ = make_runtime(lambda _: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=BytesStream([
            b'event: message_start\ndata: {"type":"message_start","message":'
            b'{"usage":{"input_tokens":10,"output_tokens":0}}}\n\n',
            b'event: content_block_delta\ndata: {"type":"content_block_delta",'
            b'"delta":{"type":"text_delta","text":"partial output"}}\n\n',
        ]),
    ))
    ledger = AsyncMock()
    ledger.reserve.return_value = "synthetic"
    monkeypatch.setattr("gateway.api.executor.FinancialLedger", lambda _: ledger)
    pool = AsyncMock()
    pool.fetchval.return_value = 1
    try:
        response = await execute_request(
            normalize_request(ClientProtocol.ANTHROPIC_MESSAGES,
                              {"model": "model-x", "max_tokens": 10, "stream": True}),
            {}, runtime, Settings(_env_file=None, durable_accounting_enabled=True),
            client_id="client-1", db_pool=pool,
        )
        content = b"".join([chunk async for chunk in response.body_iterator])
        assert b"upstream_truncated" in content
        ledger.settle.assert_awaited_once_with("synthetic", actual=None)
    finally:
        await runtime.http_client.aclose()


@pytest.mark.asyncio
async def test_background_request_is_rejected_before_dispatch():
    calls = []
    runtime, _ = make_runtime(lambda request: calls.append(request))
    try:
        response = await execute_request(
            normalize_request(ClientProtocol.OPENAI_RESPONSES,
                              {"model": "model-x", "background": True}),
            {}, runtime, Settings(_env_file=None), client_id="client-1",
        )
        assert response.status_code == 400
        assert not calls
    finally:
        await runtime.http_client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("full", [True, False])
async def test_shared_capacity_exhaustion_distinguished_from_outage(monkeypatch, full):
    from gateway.distributed_capacity import CapacityFull, CapacityUnavailable

    calls = []
    runtime, _ = make_runtime(lambda request: calls.append(request))
    controls = RouteControls({"provider-model-x": 1})
    controls.abandon = AsyncMock(wraps=controls.abandon)
    runtime = replace(runtime, route_controls=controls)
    failure = CapacityFull("capacity full") if full else CapacityUnavailable("database unavailable")
    monkeypatch.setattr("gateway.api.executor.SharedRouteControls", lambda pool, local: local)
    monkeypatch.setattr("gateway.api.executor.reserve_route_capacity",
                        AsyncMock(side_effect=failure))
    pool = AsyncMock()
    pool.fetchval.return_value = 1
    try:
        response = await execute_request(
            normalize_request(ClientProtocol.ANTHROPIC_MESSAGES,
                              {"model": "model-x", "max_tokens": 10}),
            {}, runtime, Settings(_env_file=None, distributed_admission_enabled=True),
            client_id="client-1", db_pool=pool,
        )
        assert response.status_code == (429 if full else 503)
        assert response.headers.get("Retry-After") == ("1" if full else None)
        assert getattr(response, "_gateway_capacity_deferred", False) is full
        assert not calls
        controls.abandon.assert_awaited_once()
        recovered = await controls.acquire("provider-model-x", timeout_seconds=.1)
        assert recovered is not None
        recovered.release()
    finally:
        await runtime.http_client.aclose()
