import asyncio
import json
import sqlite3
from contextlib import suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import Request

from gateway.admin.metrics import operational_health
from gateway.metrics import TRACE_LIMIT, StageMetrics, current_trace, measured, metrics, timed


@pytest.mark.asyncio
async def test_diagnostic_observer_tracks_nested_scopes_and_preserves_original_errors():
    from gateway.metrics import active_measurement, measurement_observer

    observations = []

    def observe(stage, elapsed, error):
        scope = active_measurement.get()
        observations.append((stage, scope.stage, scope.task, error))
        assert elapsed >= 0

    @measured("token_estimation")
    def estimate():
        return active_measurement.get().stage

    @measured("durable_reservation")
    async def reserve():
        outer = active_measurement.get()
        assert estimate() == "token_estimation"
        assert active_measurement.get() is outer
        await asyncio.sleep(0)
        raise failure

    failure = asyncio.CancelledError("synthetic")
    token = measurement_observer.set(observe)
    try:
        with pytest.raises(asyncio.CancelledError) as caught:
            await reserve()
        assert caught.value is failure
        assert active_measurement.get() is None
    finally:
        measurement_observer.reset(token)
    assert observations == [
        ("token_estimation", "token_estimation", asyncio.current_task(), False),
        ("durable_reservation", "durable_reservation", asyncio.current_task(), True),
    ]


def test_failed_diagnostic_observer_does_not_replace_result_or_exception():
    from gateway.metrics import active_measurement, measurement_observer

    def broken(*args):
        raise ValueError("diagnostic failed")

    token = measurement_observer.set(broken)
    try:
        with timed("routing"):
            assert active_measurement.get().stage == "routing"
        failure = RuntimeError("inference failed")
        with pytest.raises(RuntimeError) as caught, timed("routing"):
            raise failure
        assert caught.value is failure
        assert active_measurement.get() is None
    finally:
        measurement_observer.reset(token)


@pytest.mark.parametrize("stage", [
    "capacity_admission", "route_capacity_admission", "shared_circuit_allow",
    "route_concurrency_acquire", "ownership_verify", "durable_dispatch",
])
@pytest.mark.parametrize("failure", [None, RuntimeError, asyncio.CancelledError])
def test_timed_records_success_failure_and_cancellation(monkeypatch, stage, failure):
    import importlib

    module = importlib.import_module("gateway.metrics")
    local = StageMetrics()
    monkeypatch.setattr(module, "metrics", local)
    clock = iter([10., 10.125])
    monkeypatch.setattr(module, "perf_counter", lambda: next(clock))
    spans = []
    token = current_trace.set(spans)
    error = failure("synthetic") if failure else None
    try:
        if error is None:
            with timed(stage):
                pass
        else:
            with pytest.raises(failure) as caught, timed(stage):
                raise error
            assert caught.value is error
    finally:
        current_trace.reset(token)
    histogram = local.histograms[stage]
    assert histogram.count == 1
    assert histogram.errors == int(failure is not None)
    assert histogram.total_seconds == .125
    assert spans == [{"stage": stage, "seconds": .125, "error": failure is not None}]


def test_timed_rejects_unknown_stage_before_entering_body():
    with pytest.raises(ValueError, match="Unknown measurement stage"), timed("private-client-id"):
        pytest.fail("Invalid measurement stage was accepted")


def test_histogram_memory_is_constant_and_contains_no_caller_data():
    local = StageMetrics()
    for _ in range(10000):
        local.observe("routing", .002)
    snapshot = local.snapshot()
    assert snapshot["stages"]["routing"]["count"] == 10000
    assert len(snapshot["stages"]["routing"]["buckets"]) == 11
    assert len(json.dumps(snapshot)) < 6000
    with pytest.raises(ValueError):
        measured("arbitrary-client-id")


@pytest.mark.asyncio
async def test_task_local_traces_are_isolated_bounded_and_record_cancellation():
    @measured("routing")
    async def work(cancel=False):
        await asyncio.sleep(0)
        if cancel:
            raise asyncio.CancelledError

    async def run(cancel):
        spans = []
        token = current_trace.set(spans)
        try:
            for _ in range(TRACE_LIMIT + 10):
                with suppress(asyncio.CancelledError):
                    await work(cancel)
        finally:
            current_trace.reset(token)
        return spans

    successful, cancelled = await asyncio.gather(run(False), run(True))
    assert len(successful) == len(cancelled) == TRACE_LIMIT
    assert all(not span["error"] for span in successful)
    assert all(span["error"] for span in cancelled)


@pytest.mark.asyncio
async def test_metrics_require_admin_and_samples_exclude_sensitive_fields():
    from gateway.app import create_app
    from gateway.config import Settings

    class Verifier:
        async def verify(self, token):
            if token != "synthetic-admin":
                raise ValueError("bad session")
            return SimpleNamespace(subject="synthetic")

    app = create_app(Settings(_env_file=None, database_url=None, trace_sample_rate=1),
                     admin_verifier=Verifier())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        assert (await client.get("/api/admin/v1/metrics")).status_code == 401
        for _ in range(40):
            assert (await client.get("/health?secret=must-not-appear")).status_code == 200
        result = await client.get("/api/admin/v1/metrics",
                                  headers={"Authorization": "Bearer synthetic-admin"})
        assert result.status_code == 200
        assert result.json()["scope"] == "process"
        traces = await client.get("/api/admin/v1/traces",
                                 headers={"Authorization": "Bearer synthetic-admin"})
        assert len(traces.json()["samples"]) == 32
        assert "must-not-appear" not in traces.text
        assert "synthetic-admin" not in traces.text
    assert len(metrics.traces) == 32


@pytest.mark.asyncio
async def test_predispatch_timing_excludes_upstream_delay_and_unscoped_worker_calls():
    from time import perf_counter

    from gateway.api.executor import execute_request
    from gateway.config import Settings
    from gateway.metrics import request_started
    from gateway.protocols import ClientProtocol, normalize_request
    from tests.test_messages_api import make_runtime

    histogram = metrics.histograms["gateway_pre_dispatch"]
    initial = histogram.count

    async def upstream(_):
        # Observation must already exist before even entering the HTTP transport.
        assert histogram.count == initial + 1
        await asyncio.sleep(.02)
        return httpx.Response(200, json={"type": "message", "content": [],
            "usage": {"input_tokens": 1, "output_tokens": 1}})

    runtime, _ = make_runtime(upstream)
    normalized = normalize_request(ClientProtocol.ANTHROPIC_MESSAGES,
                                   {"model": "model-x", "max_tokens": 10})
    token = request_started.set(perf_counter())
    try:
        response = await execute_request(normalized, {}, runtime, Settings(_env_file=None),
                                         client_id="client-1")
        assert response.status_code == 200
    finally:
        request_started.reset(token)
    try:
        response = await execute_request(normalized, {}, runtime, Settings(_env_file=None),
                                         client_id="client-1")
        assert response.status_code == 200
        assert histogram.count == initial + 1
    finally:
        await runtime.http_client.aclose()


def operational_request(pool, *, workflows=False, accounting=False, distributed=True):
    state = SimpleNamespace(
        db_pool=pool, ready=True, drain=SimpleNamespace(draining=False, active=0),
        health_recorder=None,
        settings=SimpleNamespace(workflows_enabled=workflows, durable_accounting_enabled=accounting,
                                 distributed_admission_enabled=distributed),
        admin_verifier=SimpleNamespace(verify=AsyncMock(return_value=SimpleNamespace(subject="admin"))),
    )
    return Request({"type": "http", "app": SimpleNamespace(state=state),
                    "headers": [(b"authorization", b"Bearer synthetic-admin")]})


def operational_pool(*, size=2, idle=1, maximum=2):
    return SimpleNamespace(
        get_size=lambda: size, get_idle_size=lambda: idle, get_max_size=lambda: maximum,
        fetchval=AsyncMock(return_value=1), fetch=AsyncMock(return_value=[]),
        fetchrow=AsyncMock(return_value={"unknown_usage": 0, "settlement_backlog": 0}),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("distributed", [False, True])
async def test_operational_health_probes_with_database_features_disabled(distributed):
    pool = operational_pool()
    response = await operational_health(operational_request(pool, distributed=distributed))
    assert response.status_code == 200
    assert json.loads(response.body)["database"] == "available"
    pool.fetchval.assert_awaited_once_with("select 1")
    pool.fetch.assert_not_awaited()
    pool.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_operational_health_reports_failed_probe_and_skips_aggregates():
    pool = operational_pool()
    pool.fetchval.side_effect = RuntimeError("private database details")
    response = await operational_health(operational_request(pool, workflows=True, accounting=True))
    result = json.loads(response.body)
    assert result["database"] == "unavailable"
    assert result["warnings"] == ["database_unavailable"]
    assert b"private database details" not in response.body
    pool.fetch.assert_not_awaited()
    pool.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("stalled_method", ["fetchval", "fetch", "fetchrow"])
async def test_operational_health_bounds_probe_and_aggregates_with_one_budget(
    monkeypatch, stalled_method,
):
    import gateway.admin.metrics as admin_metrics

    budgets = []

    def short_timeout(seconds):
        budgets.append(seconds)
        return asyncio.timeout(.02)

    monkeypatch.setattr(admin_metrics, "asyncio", SimpleNamespace(timeout=short_timeout))
    cancelled = asyncio.Event()

    async def stall(*args):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    pool = operational_pool(size=2, idle=0)
    getattr(pool, stalled_method).side_effect = stall
    response = await asyncio.wait_for(
        operational_health(operational_request(pool, workflows=True, accounting=True)), 1,
    )
    result = json.loads(response.body)
    assert budgets == [2]  # A single two-second budget, not one timeout per query.
    assert cancelled.is_set()
    assert result["database"] == "unavailable"
    assert result["warnings"] == ["pool_exhausted", "database_unavailable"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("size", "idle", "exhausted"), [(2, 0, True), (1, 0, False), (2, 1, False)],
)
async def test_operational_health_warns_only_when_pool_has_no_capacity(size, idle, exhausted):
    pool = operational_pool(size=size, idle=idle)
    result = json.loads((await operational_health(operational_request(pool))).body)
    assert result["database"] == "available"
    assert result["pool"] == {"size": size, "idle": idle, "maximum": 2}
    assert ("pool_exhausted" in result["warnings"]) is exhausted


@pytest.mark.asyncio
@pytest.mark.parametrize("queue_age", [5, 60, 61])
async def test_operational_workflow_query_uses_requeue_age_and_excludes_terminal_history(queue_age):
    # Execute the actual grouping/filter/CASE offline; only adapt PostgreSQL's epoch extraction.
    with sqlite3.connect(":memory:") as connection:
        connection.row_factory = sqlite3.Row
        connection.create_function("now", 0, lambda: 10000)
        connection.execute("attach database ':memory:' as public")
        connection.execute(
            "create table public.workflow_jobs(status text, created_at integer, updated_at integer)"
        )
        connection.executemany("insert into public.workflow_jobs values (?, ?, ?)", [
            ("queued", 0, 10000 - queue_age), ("queued", 0, 9999),
            ("running", 0, 9999), ("waiting_for_tool", 0, 9999),
            ("waiting_for_approval", 0, 9999), ("completed", 0, 0),
            ("failed", 0, 0), ("cancelled", 0, 0), ("expired", 0, 0),
        ])

        async def fetch(query):
            return connection.execute(query.replace("extract(epoch from ", "(")).fetchall()

        pool = operational_pool()
        pool.fetch.side_effect = fetch
        result = json.loads((await operational_health(
            operational_request(pool, workflows=True),
        )).body)
    assert result["database"] == "available"
    rows = {row["status"]: row for row in result["workflows"]}
    assert set(rows) == {"queued", "running", "waiting_for_tool", "waiting_for_approval"}
    assert rows["queued"] == {"status": "queued", "count": 2, "oldest_seconds": queue_age}
    assert rows["running"]["oldest_seconds"] == 10000
    assert ("workflow_queue_age" in result["warnings"]) is (queue_age > 60)


@pytest.mark.asyncio
async def test_operational_health_preserves_accounting_warnings():
    pool = operational_pool()
    pool.fetchrow.return_value = {"unknown_usage": 2, "settlement_backlog": 3}
    result = json.loads((await operational_health(operational_request(pool, accounting=True))).body)
    assert result["database"] == "available"
    assert result["accounting"] == {"unknown_usage": 2, "settlement_backlog": 3}
    assert result["warnings"] == ["unknown_usage", "settlement_backlog"]


@pytest.mark.asyncio
async def test_operational_health_unconfigured_and_unauthorized():
    result = json.loads((await operational_health(operational_request(None))).body)
    assert result["database"] == "unconfigured"
    assert "pool" not in result
    pool = operational_pool()
    request = operational_request(pool)
    request.scope["headers"] = []
    assert (await operational_health(request)).status_code == 401
    pool.fetchval.assert_not_awaited()


@pytest.mark.asyncio
async def test_operational_health_preserves_external_cancellation():
    pool = operational_pool()
    pool.fetchval.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await operational_health(operational_request(pool))
