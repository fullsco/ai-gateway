import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.responses import Response

from gateway.config import Settings
from gateway.protocols import ClientProtocol
from gateway.workflow_store import LeaseLost
from gateway.workflow_worker import WorkflowWorker, checkpoint_for_tools, prepare_request
from tests.test_messages_api import make_runtime


def job(protocol=ClientProtocol.ANTHROPIC_MESSAGES):
    return {"id": uuid4(), "client_id": "client-1", "version": 1,
            "deadline": datetime.now(UTC) + timedelta(seconds=60), "checkpoint": {},
            "input": {"protocol": protocol.value, "max_steps": 4,
                      "request": {"model": "model-x", "messages": [], "max_tokens": 10}}}


@pytest.mark.asyncio
async def test_worker_completes_with_persisted_fenced_result(monkeypatch):
    runtime, _ = make_runtime(lambda _: None)
    worker = WorkflowWorker(None, runtime, Settings(_env_file=None))
    worker.store = AsyncMock()
    item = job()
    response = {"type": "message", "content": [{"type": "text", "text": "synthetic"}]}
    executor = AsyncMock(return_value=Response(json.dumps(response)))
    monkeypatch.setattr("gateway.workflow_worker.execute_request", executor)
    try:
        await worker._execute(item)
        args = worker.store.transition.call_args.args
        assert args[:4] == (item["id"], worker.owner, 1, "completed")
        assert args[-1] == response
        assert executor.call_args.kwargs["client_id"] == "client-1"
    finally:
        await runtime.http_client.aclose()


@pytest.mark.asyncio
async def test_failed_inference_checkpoint_consumes_tool_result_exactly_once(monkeypatch):
    runtime, _ = make_runtime(lambda _: None)
    worker = WorkflowWorker(None, runtime, Settings(_env_file=None))
    worker.store = AsyncMock()
    item = job()
    item["checkpoint"] = {
        "step": 1, "request": item["input"]["request"], "invocation_id": "tool-a",
        "tool_result": "synthetic-result", "pending_tools": [{"id": "tool-a"}],
    }
    monkeypatch.setattr("gateway.workflow_worker.execute_request",
                        AsyncMock(return_value=Response("{}", status_code=503)))
    try:
        await worker._execute(item)
        checkpoint = worker.store.transition.call_args.args[4]
        assert "tool_result" not in checkpoint
        resumed = {**item, "checkpoint": checkpoint}
        _, request = prepare_request(resumed)
        assert json.dumps(request).count("synthetic-result") == 1
    finally:
        await runtime.http_client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("deferred", [True, False])
async def test_worker_retries_only_proven_predispatch_capacity_refusal(monkeypatch, deferred):
    runtime, _ = make_runtime(lambda _: None)
    worker = WorkflowWorker(None, runtime, Settings(_env_file=None))
    worker.store = AsyncMock()
    refused = Response("{}", status_code=429)
    if deferred:
        refused._gateway_capacity_deferred = True
    executor = AsyncMock(side_effect=[refused, Response('{"content": []}')])
    monkeypatch.setattr("gateway.workflow_worker.execute_request", executor)
    try:
        await worker._execute(job())
        assert executor.await_count == (2 if deferred else 1)
        assert worker.store.transition.call_args.args[3] == (
            "completed" if deferred else "waiting_for_approval"
        )
    finally:
        await runtime.http_client.aclose()


@pytest.mark.asyncio
async def test_worker_capacity_retry_budget_is_bounded(monkeypatch):
    runtime, _ = make_runtime(lambda _: None)
    worker = WorkflowWorker(None, runtime, Settings(_env_file=None))
    worker.store = AsyncMock()
    refused = Response("{}", status_code=429)
    refused._gateway_capacity_deferred = True
    executor = AsyncMock(return_value=refused)
    monkeypatch.setattr("gateway.workflow_worker.execute_request", executor)
    try:
        await worker._execute(job())
        assert executor.await_count == 8
        assert worker.store.transition.call_args.args[3] == "waiting_for_approval"
    finally:
        await runtime.http_client.aclose()


@pytest.mark.parametrize("protocol,response", [
    (ClientProtocol.ANTHROPIC_MESSAGES, {"content": [
        {"type": "tool_use", "id": "a", "name": "test", "input": {}},
        {"type": "tool_use", "id": "b", "name": "test", "input": {}},
    ]}),
    (ClientProtocol.OPENAI_CHAT_COMPLETIONS, {"choices": [{"message": {
        "role": "assistant", "tool_calls": [{"id": "a"}, {"id": "b"}],
    }}]}),
    (ClientProtocol.OPENAI_RESPONSES, {"output": [
        {"type": "function_call", "call_id": "a"},
        {"type": "function_call", "call_id": "b"},
    ]}),
])
def test_tool_checkpoint_preserves_all_calls_and_adds_matching_result(protocol, response):
    item = job(protocol)
    checkpoint = checkpoint_for_tools(protocol, item["input"]["request"], response, 1)
    assert checkpoint["invocation_id"] == "a"
    assert len(checkpoint["pending_tools"]) == 2
    item["checkpoint"] = {**checkpoint, "tool_result": "synthetic-result"}
    _, prepared = prepare_request(item)
    assert "synthetic-result" in json.dumps(prepared)
    assert "synthetic-result" not in json.dumps(checkpoint)


@pytest.mark.asyncio
async def test_lost_worker_lease_cancels_step_and_never_writes_result():
    worker = WorkflowWorker(None, None, SimpleNamespace())
    worker.store = AsyncMock()
    worker.store.claim.return_value = job()
    worker.store.renew.side_effect = LeaseLost("lost")
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def step(_):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    worker._execute = step
    task = asyncio.create_task(worker.run_once())
    await started.wait()
    # Cancel the service owner, simulating shutdown; its child must be drained.
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
    worker.store.transition.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["refused", "stalled"])
async def test_actual_heartbeat_failure_cancels_running_step(failure):
    worker = WorkflowWorker(None, None, SimpleNamespace())
    worker.store = AsyncMock()
    worker.store.claim.return_value = job()
    cancelled = asyncio.Event()

    async def step(_):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def renew(*args, **kwargs):
        if failure == "refused":
            raise LeaseLost("synthetic fencing refusal")
        await asyncio.Event().wait()

    worker._execute = step
    worker.store.renew.side_effect = renew
    if failure == "stalled":
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(worker.run_once(), timeout=22)
    else:
        assert await asyncio.wait_for(worker.run_once(), timeout=10)
    worker.store.renew.assert_awaited_once()
    assert cancelled.is_set()
    worker.store.transition.assert_not_called()
