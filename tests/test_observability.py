import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.api.executor import _stream_response, _StreamFinalizer
from gateway.app import create_app
from gateway.config import Settings
from gateway.observability import (
    PassiveHealthEvent,
    PassiveHealthRecorder,
    RequestRecorder,
    StreamUsageAccumulator,
    _passive_health,
    extract_usage,
)
from gateway.protocols import ClientProtocol
from tests.test_failover_api import BytesStream, make_runtime


class TelemetryPool:
    def __init__(self) -> None:
        self.execute_calls: list[tuple[str, tuple[object, ...]]] = []
        self.attempt_id = 40

    async def execute(self, query: str, *args: object) -> None:
        self.execute_calls.append((query, args))

    async def fetchval(self, query: str, *args: object) -> int:
        self.execute_calls.append((query, args))
        self.attempt_id += 1
        return self.attempt_id


def test_extracts_anthropic_and_openai_usage_without_content() -> None:
    anthropic = extract_usage(
        ClientProtocol.ANTHROPIC_MESSAGES,
        json.dumps(
            {
                "content": [{"type": "text", "text": "must not be retained"}],
                "usage": {
                    "input_tokens": 12,
                    "output_tokens": 8,
                    "cache_read_input_tokens": 4,
                },
            }
        ).encode(),
    )
    openai = extract_usage(
        ClientProtocol.OPENAI_RESPONSES,
        json.dumps(
            {
                "output": [{"content": "must not be retained"}],
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "input_tokens_details": {"cached_tokens": 2},
                },
            }
        ).encode(),
    )

    assert anthropic == (12, 8, 4)
    assert openai == (10, 5, 2)


def test_stream_usage_accumulator_reads_split_sse_metadata() -> None:
    accumulator = StreamUsageAccumulator(ClientProtocol.ANTHROPIC_MESSAGES)

    accumulator.feed(b'event: message_start\ndata: {"usage":{"input_tokens":9}}\n\n')
    accumulator.feed(b'event: message_delta\ndata: {"usage":{"output_')
    accumulator.feed(b'tokens":6}}\n\n')

    assert accumulator.usage == (9, 6, None)


@pytest.mark.parametrize(
    ("category", "health"),
    [
        (None, "healthy"),
        ("upstream_authentication_error", "auth_failed"),
        ("quota_exhausted", "quota_exhausted"),
        ("rate_limit", "rate_limited"),
        ("timeout", "degraded"),
        ("provider_unavailable", "unavailable"),
        ("invalid_request", None),
        ("model_unavailable", None),
    ],
)
def test_passive_health_classification(category, health) -> None:
    assert _passive_health(category) == health


@pytest.mark.asyncio
async def test_passive_health_worker_persists_only_safe_metadata() -> None:
    pool = TelemetryPool()
    recorder = PassiveHealthRecorder(pool)
    recorder.start()
    observed = datetime.now(UTC)

    recorder.submit(
        PassiveHealthEvent(
            provider_id="provider",
            credential_id="credential",
            provider_model_id="provider-model",
            request_id="gw_safe_request",
            attempt_number=2,
            observed_at=observed,
            latency_ms=12.5,
            error_category="rate_limit",
            upstream_status=429,
            retry_after_seconds=5,
        )
    )
    await recorder.close()

    query, args = pool.execute_calls[-1]
    assert "provider_events" in query
    assert args[0] == "credential"
    assert args[2] == "rate_limit"
    assert args[3] == "rate_limited"
    assert args[4] == observed + timedelta(seconds=5)
    assert args[-2:] == ("gw_safe_request", "provider-model")
    assert "prompt" not in repr(args).lower()


def test_passive_health_queue_saturation_drops_without_raising() -> None:
    recorder = PassiveHealthRecorder(TelemetryPool(), max_queue_size=1)
    event = PassiveHealthEvent(
        provider_id="provider",
        credential_id="credential",
        provider_model_id="provider-model",
        request_id="request",
        attempt_number=1,
        observed_at=datetime.now(UTC),
        latency_ms=1,
    )

    recorder.submit(event)
    recorder.submit(event)

    assert recorder._queue.qsize() == 1


def test_request_attempt_and_usage_are_persisted_without_payload_content() -> None:
    pool = TelemetryPool()

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "type": "message",
                "content": [{"type": "text", "text": "sensitive generated content"}],
                "usage": {"input_tokens": 3, "output_tokens": 2},
            },
        )

    runtime, key = make_runtime(handler)
    app = create_app(Settings(environment="test", _env_file=None), runtime, db_pool=pool)

    with TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            headers={"x-api-key": key},
            json={"model": "model-x", "messages": [{"role": "user", "content": "secret"}]},
        )

    assert response.status_code == 200
    queries = "\n".join(query for query, _ in pool.execute_calls)
    arguments = repr([args for _, args in pool.execute_calls])
    assert "insert into public.request_logs" in queries
    assert "insert into public.request_attempts" in queries
    assert "insert into public.usage_records" in queries
    assert "update public.request_logs" in queries
    assert "sensitive generated content" not in arguments
    assert "secret" not in arguments
    usage_args = next(args for query, args in pool.execute_calls if "usage_records" in query)
    assert usage_args[-3:] == (3, 2, None)


def test_telemetry_failure_does_not_fail_inference() -> None:
    class FailingPool(TelemetryPool):
        async def execute(self, query: str, *args: object) -> None:
            raise RuntimeError("database unavailable")

        async def fetchval(self, query: str, *args: object) -> int:
            raise RuntimeError("database unavailable")

    runtime, key = make_runtime(lambda _: httpx.Response(200, json={"type": "message"}))
    app = create_app(
        Settings(environment="test", _env_file=None),
        runtime,
        db_pool=FailingPool(),
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            headers={"x-api-key": key},
            json={"model": "model-x", "messages": []},
        )

    assert response.status_code == 200


def test_same_provider_key_rotation_is_retry_not_provider_fallback() -> None:
    pool = TelemetryPool()
    requests = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            return httpx.Response(429, json={"error": {"message": "rate limited"}})
        return httpx.Response(200, json={"type": "message"})

    runtime, key = make_runtime(handler)
    app = create_app(Settings(environment="test", _env_file=None), runtime, db_pool=pool)

    with TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            headers={"x-api-key": key},
            json={"model": "model-x", "messages": []},
        )

    assert response.status_code == 200
    request_update = next(
        args for query, args in pool.execute_calls if "update public.request_logs" in query
    )
    assert request_update[5] == 1
    assert request_update[6] == 0


def test_completed_stream_persists_committed_attempt_and_usage() -> None:
    pool = TelemetryPool()
    stream = [
        b'event: message_start\ndata: {"usage":{"input_tokens":7}}\n\n',
        b'event: message_delta\ndata: {"usage":{"output_tokens":4}}\n\n',
    ]
    runtime, key = make_runtime(
        lambda _: httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=BytesStream(stream),
        )
    )
    app = create_app(Settings(environment="test", _env_file=None), runtime, db_pool=pool)

    with TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            headers={"x-api-key": key},
            json={"model": "model-x", "stream": True, "messages": []},
        )

    assert response.status_code == 200
    attempt_update = next(
        args for query, args in pool.execute_calls if "update public.request_attempts" in query
    )
    assert attempt_update[1] == "succeeded"
    assert attempt_update[4] is True
    usage_args = next(args for query, args in pool.execute_calls if "usage_records" in query)
    assert usage_args[-3:] == (7, 4, None)


async def _delayed_stream():
    await asyncio.sleep(60)
    yield b"never reached"


@pytest.mark.asyncio
async def test_cancelled_stream_finishes_committed_telemetry() -> None:
    pool = TelemetryPool()
    recorder = RequestRecorder(pool, "cancelled-request")
    response = httpx.Response(200, headers={"content-type": "text/event-stream"})
    now = datetime.now(UTC)
    finalizer = _StreamFinalizer(
        recorder,
        response=response,
        attempt_id=41,
        protocol=ClientProtocol.OPENAI_RESPONSES,
        resolved_model="model-x",
        started_at=now,
        attempt_started_at=now,
        attempts=1,
        fallback_count=0,
    )
    stream = _stream_response(
        b'data: {"type":"response.created"}\n\n',
        _delayed_stream(),
        finalizer,
    )

    first_chunk_received = asyncio.Event()

    async def consume() -> None:
        async for _ in stream:
            first_chunk_received.set()

    task = asyncio.create_task(consume())
    await first_chunk_received.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    attempt_update = next(
        args for query, args in pool.execute_calls if "update public.request_attempts" in query
    )
    request_update = next(
        args for query, args in pool.execute_calls if "update public.request_logs" in query
    )
    assert attempt_update[1] == "cancelled"
    assert attempt_update[4] is True
    assert request_update[1] == "cancelled"
