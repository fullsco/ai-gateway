import asyncio
import json
import sqlite3
from dataclasses import asdict
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

import gateway.admin.metrics as admin_metrics
from gateway.operating_envelope import OperatingEnvelopePolicy
from tests.test_metrics import operational_pool, operational_request


def request_for(pool, *, enabled=True, policy=None):
    request = operational_request(pool, workflows=enabled, accounting=enabled,
                                  distributed=enabled)
    state = request.app.state
    state.settings.max_in_flight_requests = 10
    state.settings.distributed_provider_capacity = 4
    if policy is not None:
        state.operating_envelope_policy = policy
    return request


def populated_pool():
    pool = operational_pool(size=3, idle=1, maximum=5)
    pool.fetch.return_value = [
        {"status": "queued", "count": 2, "oldest_seconds": Decimal("6.5")},
        {"status": "running", "count": 1, "oldest_seconds": Decimal("900")},
    ]
    pool.fetchrow.side_effect = [
        {"unknown_usage": 0, "settlement_backlog": 3,
         "oldest_settlement_seconds": Decimal("12.5")},
        {"provider_utilization": Decimal("0.25")},
    ]
    return pool


async def health(request):
    response = await admin_metrics.operational_health(request)
    assert response.status_code == 200
    return json.loads(response.body)


def assert_advisory(envelope):
    assert envelope["advisory_only"] is True
    assert envelope["scale_out_authorized"] is False
    assert envelope["recommendation"] == "investigate"
    for name in ("config_age_seconds", "event_loop_lag_seconds"):
        assert envelope["statuses"][name] == {
            "status": "unknown", "value": None, "reason": "missing_observation",
        }
        assert f"{name}_unknown" in envelope["reasons"]
    assert envelope["missing_signal_reasons"]["config_age_seconds"] == (
        "successful_config_refresh_time_unavailable"
    )
    assert envelope["missing_signal_reasons"]["event_loop_lag_seconds"] == (
        "recent_lag_window_unavailable"
    )


@pytest.mark.asyncio
async def test_http_envelope_preserves_payload_and_reports_only_supported_scope(monkeypatch):
    pool = populated_pool()
    policy = OperatingEnvelopePolicy(current_replicas=2, max_replicas=3,
                                     connection_budget=40)
    request = request_for(pool, policy=policy)
    request.app.state.drain.active = 9
    request.app.state.health_recorder = SimpleNamespace(dropped_events=2, write_failures=3)
    clock = iter([100.0, 100.125])
    monkeypatch.setattr(admin_metrics, "monotonic", lambda: next(clock))
    app = FastAPI()
    app.state = request.app.state
    app.include_router(admin_metrics.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as c:
        response = await c.get("/api/admin/v1/operations/health",
                               headers={"Authorization": "Bearer synthetic-admin"})
    assert response.status_code == 200
    result = response.json()
    assert result["ready"] is True
    assert result["draining"] is False
    assert result["active_requests"] == 9
    assert result["database"] == "available"
    assert result["passive_health"] == {"dropped": 2, "write_failures": 3}
    assert result["warnings"] == ["health_events_dropped", "settlement_backlog"]
    assert result["workflows"][0]["oldest_seconds"] == 6.5
    assert result["accounting"]["oldest_settlement_seconds"] == 12.5
    envelope = result["operating_envelope"]
    assert_advisory(envelope)
    assert envelope["scope"] == {
        "process": "local", "readiness": "process_and_database_only",
        "deployment": "unknown", "dependency_healthy": "database_only",
        "provider_utilization": "peak_registered_provider_lease_utilization",
    }
    assert envelope["readiness"] == {"process_ready": True, "database": "available"}
    assert envelope["observation"] == {
        "window_seconds": .125, "query_budget_seconds": 2,
        "consistency": "sequential_reads_within_request_not_atomic",
    }
    assert envelope["policy"] == {
        "source": "app.state.operating_envelope_policy", "values": asdict(policy),
    }
    assert envelope["current_replicas"] == {"value": 2, "source": "supplied_policy"}
    assert envelope["connection_budget_basis"] == "policy_estimate_not_measured_deployment"
    assert envelope["connection_budget"] == {
        "current": 20, "projected": 25, "total": 40, "reserve": 5,
        "usable": 35, "scale_out_fits": True,
    }
    values = {name: entry["value"] for name, entry in envelope["statuses"].items()}
    assert values == {
        "active_request_utilization": .9, "db_pool_utilization": .4,
        "queue_age_seconds": 6.5, "oldest_settlement_seconds": 12.5,
        "settlement_backlog": 3, "unknown_usage": 0, "provider_utilization": .25,
        "dependency_healthy": True, "config_age_seconds": None, "event_loop_lag_seconds": None,
    }
    assert envelope["statuses"]["queue_age_seconds"]["status"] == "warning"
    assert envelope["statuses"]["active_request_utilization"]["status"] == "warning"
    pool.fetchval.assert_awaited_once_with("select 1")
    assert pool.fetchrow.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(("ready", "draining"), [(True, False), (False, False), (True, True)])
async def test_unconfigured_database_does_not_invent_replica_or_dependency_health(ready, draining):
    request = request_for(None)
    request.app.state.ready = ready
    request.app.state.drain.draining = draining
    result = await health(request)
    envelope = result["operating_envelope"]
    assert_advisory(envelope)
    assert envelope["readiness"] == {
        "process_ready": ready and not draining, "database": "unconfigured",
    }
    assert result["ready"] == ready
    assert result["draining"] == draining
    assert envelope["current_replicas"] == {"value": None, "source": "unknown"}
    assert envelope["policy"]["source"] == "provisional_default"
    assert "current_replicas_unknown" in envelope["reasons"]
    for name in ("dependency_healthy", "db_pool_utilization", "queue_age_seconds",
                 "unknown_usage", "settlement_backlog", "oldest_settlement_seconds",
                 "provider_utilization"):
        assert envelope["statuses"][name]["status"] == "unknown"


@pytest.mark.asyncio
async def test_disabled_features_and_lifetime_lag_are_not_healthy_observations(monkeypatch):
    local_metrics = admin_metrics.metrics.__class__()
    local_metrics.observe("event_loop_lag", 999)
    monkeypatch.setattr(admin_metrics, "metrics", local_metrics)
    pool = operational_pool()
    request = request_for(pool, enabled=False)
    del request.app.state.settings.max_in_flight_requests
    request.app.state.runtime_manager = SimpleNamespace(version=123)
    result = await health(request)
    envelope = result["operating_envelope"]
    assert_advisory(envelope)
    assert envelope["statuses"]["dependency_healthy"]["value"] is True
    for name in ("queue_age_seconds", "unknown_usage", "settlement_backlog",
                 "oldest_settlement_seconds", "provider_utilization",
                 "active_request_utilization"):
        assert envelope["statuses"][name]["status"] == "unknown"
    pool.fetchval.assert_awaited_once_with("select 1")
    pool.fetch.assert_not_awaited()
    pool.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(("rows", "age"), [
    ([], 0),
    ([{"status": "running", "oldest_seconds": Decimal("900")}], 0),
    ([{"status": "queued", "oldest_seconds": Decimal("2.5")},
      {"status": "queued", "oldest_seconds": Decimal("5")}], 5),
    ([{"status": "queued", "oldest_seconds": Decimal("2.5")},
      {"status": "queued"}], None),
])
async def test_queue_empty_is_zero_but_missing_age_is_unknown(rows, age):
    pool = operational_pool()
    pool.fetch.return_value = rows
    result = await health(request_for(pool))
    entry = result["operating_envelope"]["statuses"]["queue_age_seconds"]
    assert entry["value"] == age
    assert entry["status"] == ("unknown" if age is None else "warning" if age >= 5 else "healthy")


@pytest.mark.asyncio
async def test_missing_accounting_age_and_unobserved_provider_are_unknown():
    pool = operational_pool()
    pool.fetchrow.side_effect = [
        {"unknown_usage": 2, "settlement_backlog": 100}, {"provider_utilization": None},
    ]
    result = await health(request_for(pool))
    envelope = result["operating_envelope"]
    assert_advisory(envelope)
    assert result["warnings"] == ["unknown_usage", "settlement_backlog"]
    assert "unknown_usage_present" in envelope["reasons"]
    for name in ("unknown_usage", "settlement_backlog"):
        assert envelope["statuses"][name]["status"] == "warning"
    for name in ("oldest_settlement_seconds", "provider_utilization"):
        assert envelope["statuses"][name]["status"] == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("probe", [None, False, True, 0, 2, "1", 1.0])
async def test_invalid_probe_cannot_report_healthy_database(probe):
    pool = operational_pool()
    pool.fetchval.return_value = probe
    result = await health(request_for(pool))
    envelope = result["operating_envelope"]
    assert_advisory(envelope)
    assert result["database"] == "unavailable"
    assert envelope["statuses"]["dependency_healthy"]["value"] is False
    pool.fetch.assert_not_awaited()
    pool.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [False, True])
async def test_provider_failure_discards_partial_evidence_and_shares_query_budget(
    monkeypatch, timeout,
):
    budgets, order = [], []
    cancelled = asyncio.Event()

    def short_timeout(seconds):
        budgets.append(seconds)
        return asyncio.timeout(.02)

    monkeypatch.setattr(admin_metrics, "asyncio", SimpleNamespace(timeout=short_timeout))
    pool = populated_pool()

    async def fetchrow(query, *args):
        order.append(query)
        if not args:
            return {"unknown_usage": 0, "settlement_backlog": 0, "oldest_settlement_seconds": 0}
        if timeout:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        raise RuntimeError("private provider query details")

    pool.fetchrow.side_effect = fetchrow
    result = await asyncio.wait_for(health(request_for(pool)), 1)
    assert budgets == [2]
    assert len(order) == 2
    assert cancelled.is_set() is timeout
    assert result["database"] == "unavailable"
    assert result["workflows"]  # Preserve legacy data, but don't endorse partial observations.
    assert result["accounting"]["settlement_backlog"] == 0
    assert "private provider query details" not in json.dumps(result)
    envelope = result["operating_envelope"]
    assert_advisory(envelope)
    assert envelope["statuses"]["dependency_healthy"]["value"] is False
    for name in ("queue_age_seconds", "unknown_usage", "settlement_backlog",
                 "oldest_settlement_seconds", "provider_utilization"):
        assert envelope["statuses"][name]["status"] == "unknown"


@pytest.mark.asyncio
async def test_provider_query_cancellation_propagates():
    pool = populated_pool()
    pool.fetchrow.side_effect = [{"unknown_usage": 0}, asyncio.CancelledError()]
    with pytest.raises(asyncio.CancelledError):
        await health(request_for(pool))


@pytest.mark.asyncio
@pytest.mark.parametrize(("error", "status"), [(ValueError, 401), (PermissionError, 403)])
async def test_rejected_admin_never_observes_database(error, status):
    pool = populated_pool()
    request = request_for(pool)
    request.app.state.admin_verifier.verify.side_effect = error("private auth details")
    response = await admin_metrics.operational_health(request)
    assert response.status_code == status
    assert b"operating_envelope" not in response.body
    assert b"private auth details" not in response.body
    pool.fetchval.assert_not_awaited()
    pool.fetch.assert_not_awaited()
    pool.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(("registered", "leases", "expected"), [
    ([], [], None),
    ([("provider:idle", 4)], [], 0),
    ([("provider:busy", 10), ("provider:idle", 4)],
     [("one", "provider:busy", 101), ("two", "provider:busy", 101),
      ("expired", "provider:busy", 99), ("boundary", "provider:busy", 100)], .5),
    ([("provider:limited", 2)], [("one", "provider:limited", 101)], .5),
    ([("provider:over", 1)],
     [("one", "provider:over", 101), ("two", "provider:over", 101)], 1),
])
async def test_provider_sql_is_read_only_and_uses_peak_live_effective_capacity(
    registered, leases, expected,
):
    with sqlite3.connect(":memory:") as connection:
        connection.row_factory = sqlite3.Row
        connection.create_function("now", 0, lambda: 100)
        connection.create_function("least", 2, min)
        connection.execute("attach database ':memory:' as public")
        connection.execute("create table public.capacity_scopes(scope text, capacity integer)")
        connection.execute(
            "create table public.capacity_leases(owner text, scope text, expires_at integer)"
        )
        connection.executemany(
            "insert into public.capacity_scopes values (?, ?)",
            [*registered, ("route:busy", 1), ("workflow-share:provider:busy", 1)],
        )
        connection.executemany("insert into public.capacity_leases values (?, ?, ?)",
                               [*leases, ("other", "route:busy", 101),
                                ("other", "workflow-share:provider:busy", 101)])
        connection.execute("pragma query_only=on")
        before = connection.total_changes

        async def fetchval(query):
            return connection.execute(query).fetchone()[0]

        async def fetchrow(query, *args):
            return connection.execute(query, {"1": args[0]}).fetchone()

        pool = operational_pool()
        pool.fetchval.side_effect = fetchval
        pool.fetchrow.side_effect = fetchrow
        request = request_for(pool, enabled=False)
        request.app.state.settings.distributed_admission_enabled = True
        result = await health(request)
        assert connection.total_changes == before
    assert result["database"] == "available"
    pool.fetchval.assert_awaited_once_with("select 1")
    assert pool.fetchrow.await_count == 1
    assert pool.fetchrow.await_args.args[1:] == (4,)
    pool.fetch.assert_not_awaited()
    envelope = result["operating_envelope"]
    assert_advisory(envelope)
    entry = envelope["statuses"]["provider_utilization"]
    assert entry["value"] == expected
    assert entry["status"] == (
        "unknown" if expected is None else "warning" if expected >= .9 else "healthy"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("unapplied_ages", [[], [12, 80]])
async def test_accounting_sql_reads_oldest_unapplied_outbox_and_unsettled_counts(unapplied_ages):
    with sqlite3.connect(":memory:") as connection:
        connection.row_factory = sqlite3.Row
        connection.create_function("now", 0, lambda: 1000)
        # PostgreSQL GREATEST ignores NULL arguments.
        connection.create_function("greatest", 2,
                                   lambda a, b: max(v for v in (a, b) if v is not None))
        connection.execute("attach database ':memory:' as public")
        connection.execute("create table public.financial_reservations(status text)")
        connection.execute(
            "create table public.settlement_outbox(created_at integer, applied_at integer)"
        )
        connection.executemany("insert into public.financial_reservations values (?)", [
            ("unknown",), ("settlement_pending",), ("settlement_pending",), ("settled",),
        ])
        connection.executemany("insert into public.settlement_outbox values (?, ?)",
                               [(0, 1), *((1000 - age, None) for age in unapplied_ages)])
        connection.execute("pragma query_only=on")

        async def fetchrow(query):
            return connection.execute(query.replace("extract(epoch from ", "(")).fetchone()

        pool = operational_pool()
        pool.fetchrow.side_effect = fetchrow
        request = request_for(pool, enabled=False)
        request.app.state.settings.durable_accounting_enabled = True
        result = await health(request)
    assert result["database"] == "available"
    assert result["accounting"] == {
        "unknown_usage": 1, "settlement_backlog": 2,
        "oldest_settlement_seconds": max(unapplied_ages, default=0),
    }
    envelope = result["operating_envelope"]
    assert_advisory(envelope)
    assert envelope["statuses"]["oldest_settlement_seconds"]["value"] == max(
        unapplied_ages, default=0,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("capacity", [None, 0])
async def test_unconfigured_provider_capacity_skips_query_and_stays_unknown(capacity):
    pool = operational_pool()
    request = request_for(pool, enabled=False)
    request.app.state.settings.distributed_admission_enabled = True
    request.app.state.settings.distributed_provider_capacity = capacity
    result = await health(request)
    envelope = result["operating_envelope"]
    assert_advisory(envelope)
    assert envelope["statuses"]["provider_utilization"]["status"] == "unknown"
    assert envelope["missing_signal_reasons"]["provider_utilization"] == (
        "provider_capacity_disabled_unconfigured_or_unobserved"
    )
    pool.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_features_disabled_probe_is_still_bounded(monkeypatch):
    budgets = []
    cancelled = asyncio.Event()

    def short_timeout(seconds):
        budgets.append(seconds)
        return asyncio.timeout(.02)

    async def stall(query):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(admin_metrics, "asyncio", SimpleNamespace(timeout=short_timeout))
    pool = operational_pool()
    pool.fetchval.side_effect = stall
    result = await asyncio.wait_for(health(request_for(pool, enabled=False)), 1)
    assert budgets == [2]
    assert cancelled.is_set()
    assert result["database"] == "unavailable"
    assert_advisory(result["operating_envelope"])
    pool.fetchval.assert_awaited_once_with("select 1")
    pool.fetch.assert_not_awaited()
    pool.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(("active", "limit", "expected"), [(12, 10, 1), (3, 0, None)])
async def test_active_utilization_saturates_and_zero_limit_is_unknown(active, limit, expected):
    request = request_for(None)
    request.app.state.drain.active = active
    request.app.state.settings.max_in_flight_requests = limit
    envelope = (await health(request))["operating_envelope"]
    assert_advisory(envelope)
    entry = envelope["statuses"]["active_request_utilization"]
    assert entry["value"] == expected
    assert entry["status"] == ("unknown" if expected is None else "warning")
