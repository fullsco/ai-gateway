import asyncio
import json
import signal
import socket
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from gateway.metrics import active_measurement, measurement_observer, metrics, timed
from scripts.qualification_tracing import (
    QUERY_LIMIT,
    RECENT_LIMIT,
    SLOW_LIMIT,
    SPAN_LIMIT,
    RequestCapture,
    RequestTraceCollector,
    RequestTraceMiddleware,
    archive_on_shutdown,
    current_capture,
    current_query,
    driver_query_observed,
    query_operation,
    traced_query,
)


@pytest.mark.parametrize(("query", "operation"), [
    ("COMMIT;", "COMMIT"), ("START TRANSACTION;", "START"),
    ("\nselect 'secret'", "SELECT"), ("/* private */ select 1", "OTHER"),
    ("SENSITIVE-TOKEN", "OTHER"), ("", "EMPTY"), ("  ", "EMPTY"),
])
def test_query_classification_has_fixed_cardinality(query, operation):
    assert query_operation(query) == operation


@pytest.mark.asyncio
async def test_delayed_driver_callbacks_remain_bound_to_request_query_and_stage():
    gate = asyncio.Barrier(2)

    @traced_query
    async def execute_query(self, sql, delay):
        await gate.wait()
        # asyncpg schedules callbacks, so they may execute after the scope is reset.
        asyncio.get_running_loop().call_soon(
            driver_query_observed, SimpleNamespace(elapsed=delay),
        )
        return delay

    async def request(sequence, stage, sql, delay):
        capture = RequestCapture(sequence)
        token = current_capture.set(capture)
        observer = measurement_observer.set(capture.observe)
        try:
            with timed(stage):
                assert await execute_query(None, sql, delay) == delay
            assert current_query.get() is None
        finally:
            measurement_observer.reset(observer)
            current_capture.reset(token)
        await asyncio.sleep(0)
        return capture.snapshot()

    first, second = await asyncio.gather(
        request(1, "capacity_admission", "SELECT 'private-a'", .001),
        request(2, "durable_reservation", "UPDATE secret SET key='private-b'", .002),
    )
    for capture, stage, operation, delay in [
        (first, "capacity_admission", "SELECT", .001),
        (second, "durable_reservation", "UPDATE", .002),
    ]:
        assert capture["missing_driver_timings"] == 0
        assert len(capture["queries"]) == len(capture["spans"]) == 1
        query = capture["queries"][0]
        span = capture["spans"][0]
        assert query["stage"] == span["stage"] == stage
        assert query["operation"] == operation
        assert query["origin"] == "stage_task"
        assert query["stage_start_offset_seconds"] == span["start_offset_seconds"]
        assert query["driver_seconds"] == delay
        assert query["driver_callback_count"] == 1
        assert 0 <= query["wall_seconds"] <= span["seconds"]
        assert span["start_offset_seconds"] <= query["start_offset_seconds"]
        assert query["end_offset_seconds"] <= span["end_offset_seconds"]
        assert "private" not in json.dumps(capture)
        assert "secret" not in json.dumps(capture)


@pytest.mark.asyncio
async def test_inherited_background_task_is_not_misattributed_to_foreground_stage():
    capture = RequestCapture(1)
    token = current_capture.set(capture)
    observer = measurement_observer.set(capture.observe)
    started = asyncio.Event()
    release = asyncio.Event()

    @traced_query
    async def query(self, sql):
        return 1

    async def background():
        started.set()
        await release.wait()
        assert await query(None, "SELECT 1") == 1

    try:
        with timed("capacity_admission"):
            task = asyncio.create_task(background())
            await started.wait()
        with timed("durable_dispatch"):
            release.set()
            await task
        entry = capture.queries[0]
        assert entry["origin"] == "inherited_task"
        assert entry["stage"] is None
        assert entry["stage_start_offset_seconds"] is None
    finally:
        measurement_observer.reset(observer)
        current_capture.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_failed_queries_and_cancelled_requests_keep_original_exception_and_reset(failure):
    collector = RequestTraceCollector(sample_rate=1)
    error = failure("private error contents")

    @traced_query
    async def query(self, sql):
        raise error

    async def app(scope, receive, send):
        with timed("durable_dispatch"):
            await query(None, "UPDATE private SET value='secret'")

    middleware = RequestTraceMiddleware(app, collector)
    with pytest.raises(failure) as caught:
        await middleware({"type": "http", "method": "POST", "path": "/v1/messages"}, None, None)
    assert caught.value is error
    assert current_capture.get() is measurement_observer.get() is active_measurement.get() is None
    assert current_query.get() is None
    trace = collector.snapshot()["recent"][0]
    assert trace["status"] == 500
    assert trace["queries"][0]["error"] is True
    assert trace["spans"][0]["error"] is True
    assert "private" not in json.dumps(trace)


@pytest.mark.asyncio
async def test_middleware_preserves_streaming_messages_and_counts_only_uploaded_bytes():
    collector = RequestTraceCollector(sample_rate=1)
    scope = {"type": "http", "method": "POST", "path": "/v1/chat/completions",
             "headers": [(b"authorization", b"Bearer sensitive")]}
    incoming = iter([
        {"type": "http.request", "body": b"private-prompt", "more_body": True},
        {"type": "http.request", "body": b"second-part", "more_body": False},
    ])
    sent = []
    messages = [
        {"type": "http.response.start", "status": 200,
         "headers": [(b"content-type", b"text/event-stream; charset=utf-8")]},
        {"type": "http.response.body", "body": b"private-response", "more_body": False},
    ]

    async def receive():
        return next(incoming)

    async def send(message):
        sent.append(message)

    async def app(scope, receive, send):
        assert (await receive())["body"] == b"private-prompt"
        assert (await receive())["body"] == b"second-part"
        metrics.observe("gateway_pre_dispatch", .2)
        for message in messages:
            await send(message)

    await RequestTraceMiddleware(app, collector)(scope, receive, send)
    assert all(actual is expected for actual, expected in zip(sent, messages, strict=True))
    trace = collector.snapshot()["slow"][0]
    assert trace["body_bytes"] == 25
    assert trace["streaming"] is True
    assert trace["status"] == 200
    assert trace["predispatch_seconds"] == .2
    assert "private" not in json.dumps(trace)
    assert "sensitive" not in json.dumps(trace)


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path", "rate"), [
    ("GET", "/health", 1), ("GET", "/api/admin/v1/traces", 1),
    ("POST", "/v1/workflows", 1), ("POST", "/v1/messages", 0),
])
async def test_unsampled_and_non_inference_paths_do_not_allocate_capture(method, path, rate):
    collector = RequestTraceCollector(sample_rate=rate)
    seen = []

    async def app(scope, receive, send):
        assert current_capture.get() is measurement_observer.get() is None
        seen.append(True)

    await RequestTraceMiddleware(app, collector)(
        {"type": "http", "method": method, "path": path}, None, None,
    )
    assert seen == [True]
    assert collector.sampled == collector.completed == 0


def test_buffers_are_bounded_and_closing_blocks_late_capture_growth():
    collector = RequestTraceCollector(sample_rate=1)
    for _ in range(RECENT_LIMIT + 5):
        capture = collector.begin()
        for _ in range(SPAN_LIMIT + 1):
            capture.observe("routing", .001, False)
        capture.observe("gateway_pre_dispatch", .2, False)
        for _ in range(QUERY_LIMIT + 3):
            capture.begin_query("SELECT 'not-retained'")
        collector.finish(capture)
        assert capture.begin_query("SELECT 1") is None
        capture.observe("routing", .001, False)
    result = collector.snapshot()
    assert collector.completed == collector.slow_completed == RECENT_LIMIT + 5
    assert len(result["recent"]) == RECENT_LIMIT
    assert len(result["slow"]) == SLOW_LIMIT
    assert result["recent"][-1]["sequence"] == RECENT_LIMIT + 5
    assert result["recent"][-1]["dropped_spans"] == 2
    assert result["recent"][-1]["dropped_queries"] == 3
    assert result["recent"][-1]["predispatch_seconds"] == .2
    assert "not-retained" not in json.dumps(result)


@pytest.mark.asyncio
async def test_uvicorn_sigterm_archives_completed_traces_during_lifespan():
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(dir="/tmp/opencode") as directory, socket.socket() as listener:
        destination = Path(directory) / "traces.json"
        listener.bind(("127.0.0.1", 0))
        listener.listen(16)
        port = listener.getsockname()[1]
        process = await asyncio.create_subprocess_exec(
            sys.executable, str(root / "scripts/dev_check.py"),
            "--script", str(Path(__file__).resolve()), str(destination), str(listener.fileno()),
            pass_fds=(listener.fileno(),), stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
                async with asyncio.timeout(15):
                    while True:
                        try:
                            response = await client.post(f"http://127.0.0.1:{port}/v1/messages")
                            break
                        except httpx.HTTPError:
                            await asyncio.sleep(.05)
                assert response.status_code == 200
            process.terminate()
            _, errors = await asyncio.wait_for(process.communicate(), 10)
            assert process.returncode in (0, -signal.SIGTERM), errors.decode()
            saved = json.loads(destination.read_text())
            assert saved["completed"] >= 1
            assert saved["recent"][-1]["status"] == 200
            assert saved["slow"][-1]["predispatch_seconds"] == .2
            assert saved["recent"][-1]["spans"][0]["stage"] == "gateway_pre_dispatch"
        finally:
            if process.returncode is None:
                process.kill()
                await process.communicate()


if __name__ == "__main__":
    import uvicorn
    from fastapi import FastAPI

    app = FastAPI()
    collector = RequestTraceCollector(sample_rate=1)
    app.add_middleware(RequestTraceMiddleware, collector=collector)
    archive_on_shutdown(app, collector, Path(sys.argv[1]))

    @app.post("/v1/messages")
    async def message():
        metrics.observe("gateway_pre_dispatch", .2)
        return {"ok": True}

    listener = socket.socket(fileno=int(sys.argv[2]))
    asyncio.run(uvicorn.Server(uvicorn.Config(app, log_level="error")).serve(sockets=[listener]))
