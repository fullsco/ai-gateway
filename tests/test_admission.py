import asyncio
import json
from collections import deque
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import StreamingResponse

from gateway.admission import InferenceAdmissionMiddleware
from gateway.context import reset_request_id, set_request_id

PATHS = ("/v1/messages", "/v1/chat/completions", "/v1/responses")
pytestmark = pytest.mark.asyncio


@pytest.fixture(params=PATHS)
def path(request):
    return request.param


def settings(**overrides):
    return SimpleNamespace(
        **{
            "max_in_flight_requests": 1,
            "max_request_body_bytes": 8,
            "max_in_flight_body_bytes": 32,
            "request_body_timeout_seconds": 1.0,
            "overload_retry_after_seconds": 3,
            **overrides,
        }
    )


def chunk(body=b"{}", more=False):
    return {"type": "http.request", "body": body, "more_body": more}


class Exchange:
    def __init__(self, path, messages=None, headers=(), method="POST"):
        self.scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": method,
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "scheme": "http",
            "server": ("test", 80),
            "client": ("test", 123),
            "headers": list(headers),
        }
        self.messages = deque([chunk()] if messages is None else messages)
        self.sent = []
        self.receive_calls = 0
        self.disconnected = asyncio.Event()
        self.receiving = asyncio.Event()
        self.first_body = asyncio.Event()

    async def receive(self):
        self.receive_calls += 1
        if self.messages:
            return self.messages.popleft()
        self.receiving.set()
        await self.disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(self, message):
        self.sent.append(message)
        if message["type"] == "http.response.body" and message.get("body"):
            self.first_body.set()

    async def run(self, app):
        await app(self.scope, self.receive, self.send)

    @property
    def status(self):
        return self.sent[0]["status"]


async def echo(scope, receive, send):
    message = await receive()
    assert message["type"] == "http.request"
    assert message["more_body"] is False
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": message["body"]})


async def probe(app, path):
    exchange = Exchange(path)
    await asyncio.wait_for(exchange.run(app), 1)
    return exchange


def assert_error(exchange, status):
    assert exchange.status == status
    headers = dict(exchange.sent[0]["headers"])
    body = exchange.sent[1]["body"]
    assert headers[b"content-type"] == b"application/json"
    assert int(headers[b"content-length"]) == len(body)
    payload = json.loads(body)
    detail = payload["error"]
    assert detail["message"]
    assert "request_id" in detail
    assert detail["type"] == ("rate_limit_error" if status == 429 else "invalid_request_error")
    if exchange.scope["path"].rstrip("/") == "/v1/messages":
        assert payload["type"] == "error"
    else:
        assert "type" not in payload
        assert detail["param"] is None
        assert detail["code"]
    if status == 429:
        assert headers[b"retry-after"] == b"3"
    else:
        assert b"retry-after" not in headers


@pytest.mark.parametrize("suffix", ["", "/"])
@pytest.mark.parametrize("length", [None, b"8", b" \t0008\t "])
async def test_valid_body_is_preserved_and_replayed_once(path, suffix, length):
    data = b' {"a":1}'
    headers = [] if length is None else [(b"Content-Length", length)]
    exchange = Exchange(path + suffix, [chunk(data[:3], True), chunk(data[3:])], headers)
    seen = []

    async def downstream(scope, receive, send):
        assert scope is exchange.scope
        seen.append(await receive())
        exchange.disconnected.set()
        seen.append(await receive())

    app = InferenceAdmissionMiddleware(downstream, settings())
    await exchange.run(app)
    assert seen == [chunk(data), {"type": "http.disconnect"}]
    assert exchange.receive_calls == 3
    assert app._in_flight == 0


async def test_aggregate_body_budget_is_shared_and_recovers_after_disconnect(path):
    app = InferenceAdmissionMiddleware(
        echo, settings(max_in_flight_requests=3, max_in_flight_body_bytes=5)
    )
    upload = Exchange(path, [chunk(b"1234", True)])
    task = asyncio.create_task(upload.run(app))
    try:
        await asyncio.wait_for(upload.receiving.wait(), 1)
        for endpoint in PATHS:
            assert_error(await probe(app, endpoint), 429)
        # One byte still fits; rejecting a larger request did not leak its charge.
        small = Exchange(path, [chunk(b"1")])
        await small.run(app)
        assert small.status == 200
        assert app._body_bytes == 4
        upload.disconnected.set()
        await asyncio.wait_for(task, 1)
        assert app._body_bytes == 0
        assert (await probe(app, path)).status == 200
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_application_registers_admission_inside_request_context(path):
    import httpx

    from gateway.app import create_app
    from gateway.config import Settings

    app = create_app(Settings(_env_file=None, database_url=None, max_request_body_bytes=1))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        result = await client.post(path, content=b"{}")
        assert result.status_code == 413  # Rejected before the missing-runtime check.
        assert result.headers["x-request-id"] == result.json()["error"]["request_id"]
        assert (await client.get("/health")).status_code == 200


@pytest.mark.parametrize("headers", [(), ((b"content-length", b"0"),)])
async def test_empty_body(path, headers):
    exchange = Exchange(path, [chunk(b"")], headers)
    await exchange.run(InferenceAdmissionMiddleware(echo, settings()))
    assert exchange.status == 200
    assert exchange.sent[1]["body"] == b""


@pytest.mark.parametrize("headers,messages,status", [
    ((), [chunk(b"1234", True), chunk(b"56789")], 413),
    (((b"transfer-encoding", b"chunked"),), [chunk(b"123456789")], 413),
    (((b"content-length", b"1"),), [chunk(b"123456789")], 413),
    (((b"content-length", b"8"),), [chunk(b"1234", True), chunk(b"56789")], 413),
    (((b"content-length", b"1"),), [chunk(b"12")], 400),
    (((b"content-length", b"3"),), [chunk(b"12")], 400),
])
async def test_actual_size_and_length_are_enforced_before_dispatch(path, headers, messages, status):
    calls = []

    async def downstream(scope, receive, send):
        calls.append(scope)
        await echo(scope, receive, send)

    app = InferenceAdmissionMiddleware(downstream, settings())
    exchange = Exchange(path, messages, headers)
    await exchange.run(app)
    assert_error(exchange, status)
    assert calls == []
    assert (await probe(app, path)).status == 200


@pytest.mark.parametrize("value,status", [
    (b"9", 413), (b"9" * 5000, 413), (b"", 400), (b"-1", 400),
    (b"+1", 400), (b"1.0", 400), (b"1_0", 400), (b"1,1", 400),
    (b"\xff", 400), (b"1\n", 400),
])
async def test_invalid_or_oversized_declared_length_rejects_without_receiving(path, value, status):
    app = InferenceAdmissionMiddleware(echo, settings())
    exchange = Exchange(path, headers=[(b"content-length", value)])
    await exchange.run(app)
    assert_error(exchange, status)
    assert exchange.receive_calls == 0
    assert (await probe(app, path)).status == 200


@pytest.mark.parametrize("second", [b"2", b"3"])
async def test_duplicate_content_length(path, second):
    exchange = Exchange(path, headers=[(b"content-length", b"2"), (b"Content-Length", second)])
    await exchange.run(InferenceAdmissionMiddleware(echo, settings()))
    assert_error(exchange, 400)
    assert exchange.receive_calls == 0


async def test_disconnect_during_body_never_dispatches(path):
    for messages in (
        [{"type": "http.disconnect"}],
        [chunk(b"{", True), {"type": "http.disconnect"}],
    ):
        exchange = Exchange(path, messages)
        app = InferenceAdmissionMiddleware(echo, settings())
        await exchange.run(app)
        assert exchange.sent == []
        assert (await probe(app, path)).status == 200


@pytest.mark.parametrize("trickle", [False, True])
async def test_total_body_timeout_and_recovery(path, trickle):
    exchange = Exchange(path, [])
    calls = 0

    async def slow_receive():
        nonlocal calls
        calls += 1
        if trickle:
            await asyncio.sleep(0.01)
            return chunk(b"", True)
        await asyncio.Event().wait()

    app = InferenceAdmissionMiddleware(echo, settings(request_body_timeout_seconds=0.05))
    await asyncio.wait_for(app(exchange.scope, slow_receive, exchange.send), 1)
    assert_error(exchange, 408)
    assert calls > 0
    assert (await probe(app, path)).status == 200


async def test_upload_acquires_slot_before_receiving_and_cancellation_releases(path):
    app = InferenceAdmissionMiddleware(echo, settings())
    upload = Exchange(path, [chunk(b"{", True)])
    task = asyncio.create_task(upload.run(app))
    try:
        await asyncio.wait_for(upload.receiving.wait(), 1)
        rejected = await probe(app, path)
        assert_error(rejected, 429)
        assert rejected.receive_calls == 0
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert (await probe(app, path)).status == 200


@pytest.mark.parametrize("ending", ["final", "exception", "cancellation", "disconnect"])
async def test_fastapi_stream_holds_shared_slot_until_asgi_returns(path, ending):
    api = FastAPI()

    async def passthrough(request, call_next):
        return await call_next(request)

    api.add_middleware(BaseHTTPMiddleware, dispatch=passthrough)
    finish = asyncio.Event()
    cleanup_started = asyncio.Event()
    cleanup_finish = asyncio.Event()
    bodies = []

    async def route(request: Request):
        bodies.append(await request.json())

        async def events():
            try:
                yield b"data: first\n\n"
                await finish.wait()
                if ending == "exception":
                    raise RuntimeError("stream failed")
                yield b"data: final\n\n"
            finally:
                cleanup_started.set()
                if ending == "final":
                    await cleanup_finish.wait()

        return StreamingResponse(events(), media_type="text/event-stream")

    for endpoint in PATHS:
        api.add_api_route(endpoint, route, methods=["POST"])

    @api.get("/health")
    async def health():
        return {"ok": True}

    app = InferenceAdmissionMiddleware(api, settings())
    stream = Exchange(path)
    task = asyncio.create_task(stream.run(app))
    try:
        await asyncio.wait_for(stream.first_body.wait(), 1)
        assert bodies == [{}]
        assert app._body_bytes == 2
        for endpoint in PATHS:
            rejected = await probe(app, endpoint + "/")
            assert_error(rejected, 429)
            assert rejected.receive_calls == 0
        health_exchange = Exchange("/health", method="GET")
        await health_exchange.run(app)
        assert health_exchange.status == 200
        if ending == "cancellation":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif ending == "disconnect":
            stream.disconnected.set()
            await asyncio.wait_for(task, 1)
        else:
            finish.set()
            if ending == "exception":
                with pytest.raises(RuntimeError, match="stream failed"):
                    await asyncio.wait_for(task, 1)
            else:
                await asyncio.wait_for(cleanup_started.wait(), 1)
                assert_error(await probe(app, path), 429)
                cleanup_finish.set()
                await asyncio.wait_for(task, 1)
                assert stream.sent[-1].get("more_body", False) is False
        assert cleanup_started.is_set()
        assert app._in_flight == 0
        assert app._body_bytes == 0
        # Finish subsequent streams normally, proving capacity is usable again.
        if ending == "exception":
            ending = "final"
        finish.set()
        cleanup_finish.set()
        assert (await probe(app, path)).status == 200
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_downstream_lifetime_includes_work_after_final_response(path):
    final_sent = asyncio.Event()
    release = asyncio.Event()

    async def downstream(scope, receive, send):
        await echo(scope, receive, send)
        final_sent.set()
        await release.wait()

    app = InferenceAdmissionMiddleware(downstream, settings())
    task = asyncio.create_task(Exchange(path).run(app))
    try:
        await asyncio.wait_for(final_sent.wait(), 1)
        assert_error(await probe(app, path), 429)
        release.set()
        await asyncio.wait_for(task, 1)
        assert (await probe(app, path)).status == 200
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_send_exception_releases_slot(path):
    app = InferenceAdmissionMiddleware(echo, settings())

    async def broken_send(message):
        raise OSError("client disconnected")

    exchange = Exchange(path)
    with pytest.raises(OSError, match="client disconnected"):
        await app(exchange.scope, exchange.receive, broken_send)
    assert (await probe(app, path)).status == 200


@pytest.mark.parametrize("scope", [
    {"type": "lifespan"}, {"type": "websocket", "path": "/v1/messages"},
    {"type": "http", "method": "GET", "path": "/v1/messages"},
    {"type": "http", "method": "POST", "path": "/health"},
    {"type": "http", "method": "POST", "path": "/v1/messages/other"},
])
async def test_non_inference_scopes_passthrough_unchanged(scope):
    seen = []
    receive, send = object(), object()

    async def downstream(actual_scope, actual_receive, actual_send):
        seen.append((actual_scope, actual_receive, actual_send))

    app = InferenceAdmissionMiddleware(downstream, settings())
    app._in_flight = 1
    await app(scope, receive, send)
    assert seen == [(scope, receive, send)]
    assert app._in_flight == 1


async def test_request_context_is_included_in_admission_error(path):
    token = set_request_id("test-admission-id")
    try:
        exchange = Exchange(path, headers=[(b"content-length", b"9")])
        await exchange.run(InferenceAdmissionMiddleware(echo, settings()))
        assert json.loads(exchange.sent[1]["body"])["error"]["request_id"] == "test-admission-id"
    finally:
        reset_request_id(token)


@pytest.mark.parametrize("failure", ["receive", "downstream_timeout"])
async def test_exceptions_propagate_and_release_capacity(path, failure):
    async def downstream(scope, receive, send):
        if failure == "downstream_timeout":
            raise TimeoutError("downstream timed out")
        await echo(scope, receive, send)

    async def broken_receive():
        raise OSError("receive failed")

    app = InferenceAdmissionMiddleware(downstream, settings())
    exchange = Exchange(path)
    receive = broken_receive if failure == "receive" else exchange.receive
    with pytest.raises(OSError):  # TimeoutError is also an OSError.
        await app(exchange.scope, receive, exchange.send)
    assert exchange.sent == []
    failure = "recovered"
    assert (await probe(app, path)).status == 200


async def test_configured_capacity_admits_two_uploads_and_rejects_third(path):
    app = InferenceAdmissionMiddleware(echo, settings(max_in_flight_requests=2))
    uploads = [Exchange(path, []), Exchange(path, [])]
    tasks = [asyncio.create_task(upload.run(app)) for upload in uploads]
    try:
        await asyncio.wait_for(asyncio.gather(*(u.receiving.wait() for u in uploads)), 1)
        assert_error(await probe(app, path), 429)
        # A separate app instance has its own counter.
        independent = InferenceAdmissionMiddleware(echo, settings())
        assert (await probe(independent, path)).status == 200
        uploads[0].disconnected.set()
        await asyncio.wait_for(tasks[0], 1)
        assert (await probe(app, path)).status == 200
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert app._in_flight == 0


@pytest.mark.parametrize("suffix", ["", "/"])
async def test_mounted_inference_enforces_admission(path, suffix):
    mounted = InferenceAdmissionMiddleware(echo, settings())
    parent = FastAPI()
    parent.mount("/gateway", mounted)
    url = "/gateway" + path + suffix
    oversized = Exchange(url, [chunk(b"123456789")])
    await oversized.run(parent)
    assert oversized.status == 413
    payload = json.loads(oversized.sent[1]["body"])
    assert (payload.get("type") == "error") == (path == "/v1/messages")
    mounted._in_flight = 1
    rejected = Exchange(url)
    await rejected.run(parent)
    assert rejected.status == 429
    assert rejected.receive_calls == 0
    mounted._in_flight = 0
    assert (await probe(parent, url)).status == 200
