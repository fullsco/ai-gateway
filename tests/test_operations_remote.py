"""Real operations SQL on the dedicated, migrated DB; every fixture write rolls back."""

from contextlib import asynccontextmanager
from dataclasses import asdict
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from gateway.app import create_app
from gateway.config import Settings
from gateway.operating_envelope import OperatingEnvelopePolicy
from gateway.schema_checks import require_circuit_schema, require_execution_schema
from gateway.workflow_store import WorkflowStore
from scripts.dev_db_check import validate_target
from tests import test_accounting_integration as accounting
from tests.test_messages_api import make_runtime

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
URL = "/api/admin/v1/operations/health"
HEADERS = {"Authorization": "Bearer synthetic-admin"}


class ObservedPool(accounting.TransactionPool):
    def __init__(self, connection, backing):
        super().__init__(connection)
        self.backing = backing
        self.queries = []

    def __getattr__(self, name):
        if name in {"get_size", "get_idle_size", "get_max_size"}:
            return getattr(self.backing, name)
        return super().__getattr__(name)

    async def fetchval(self, query, *args):
        self.queries.append(query)
        return await self.connection.fetchval(query, *args)

    async def fetch(self, query, *args):
        self.queries.append(query)
        return await self.connection.fetch(query, *args)

    async def fetchrow(self, query, *args):
        self.queries.append(query)
        return await self.connection.fetchrow(query, *args)


class Verifier:
    async def verify(self, token):
        if token != "synthetic-admin":
            raise ValueError("Invalid synthetic admin")
        return SimpleNamespace(subject="synthetic-admin")


@asynccontextmanager
async def operations_fixture(monkeypatch):
    original = accounting.create_pool
    backing = None

    async def checked_pool(dsn):
        nonlocal backing
        validate_target(dsn, "kpmxgwflgyzjvitwlfvs")
        backing = await original(dsn)
        try:
            # Fail before the accounting fixture's optional migration fallback.
            await require_execution_schema(backing)
            await require_circuit_schema(backing)
            for query in (
                "select exists(select 1 from public.settlement_outbox)",
                "select exists(select 1 from public.capacity_scopes where scope like 'provider:%')",
            ):
                assert not await backing.fetchval(query), "Operations fixtures must be isolated"
        except BaseException:
            await backing.close()
            raise
        return backing

    monkeypatch.setattr(accounting, "create_pool", checked_pool)
    async with accounting.fixture() as (ledger, db, tenant, route):
        pool = ObservedPool(db, backing)

        def no_upstream(_request):
            pytest.fail("Operations health must not contact a provider")

        runtime, _ = make_runtime(no_upstream)
        try:
            settings = Settings(
                _env_file=None, database_url=None, environment="test",
                workflows_enabled=True, durable_accounting_enabled=True,
                distributed_admission_enabled=True, distributed_provider_capacity=4,
                health_probe_enabled=False, alert_monitor_enabled=False,
                credential_usage_poll_enabled=False,
            )
            app = create_app(settings, runtime, db_pool=pool, admin_verifier=Verifier())
            app.state.ready = True
            # ASGITransport skips lifespan: no workers race the single rollback connection.
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://test", trust_env=False,
            ) as http:
                yield http, app, pool, ledger, db, tenant, route
        finally:
            await runtime.http_client.aclose()
        # WorkflowStore's nested commits are savepoints inside accounting.fixture's rollback.


async def health(http):
    response = await http.get(URL, headers=HEADERS)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["database"] == "available", result
    return result


def assert_advisory(envelope):
    assert envelope["advisory_only"] is True
    assert envelope["scale_out_authorized"] is False
    assert envelope["recommendation"] == "investigate"
    for name, reason in (
        ("config_age_seconds", "successful_config_refresh_time_unavailable"),
        ("event_loop_lag_seconds", "recent_lag_window_unavailable"),
    ):
        assert envelope["statuses"][name] == {
            "status": "unknown", "value": None, "reason": "missing_observation",
        }
        assert envelope["missing_signal_reasons"][name] == reason
        assert f"{name}_unknown" in envelope["reasons"]


@pytest.mark.parametrize("supplied_policy", [False, True])
async def test_protected_operations_probes_real_schema_and_preserves_unknowns(
    monkeypatch, supplied_policy,
):
    async with operations_fixture(monkeypatch) as (http, app, pool, *_):
        for headers in ({}, {"Authorization": "Bearer invalid"}):
            response = await http.get(URL, headers=headers)
            assert response.status_code == 401
            assert "operating_envelope" not in response.json()
        assert pool.queries == []
        policy = OperatingEnvelopePolicy(current_replicas=2, max_replicas=3, connection_budget=40)
        if supplied_policy:
            app.state.operating_envelope_policy = policy
        result = await health(http)
        assert result["ready"] is True
        assert result["draining"] is False
        assert result["workflows"] == []
        assert result["accounting"] == {
            "unknown_usage": 0, "settlement_backlog": 0, "oldest_settlement_seconds": 0,
        }
        assert pool.queries[0] == "select 1"
        assert len(pool.queries) == 4
        for table in ("workflow_jobs", "financial_reservations", "settlement_outbox",
                      "capacity_scopes", "capacity_leases"):
            assert any(f"public.{table}" in query for query in pool.queries)
        envelope = result["operating_envelope"]
        assert_advisory(envelope)
        assert envelope["statuses"]["queue_age_seconds"]["value"] == 0
        assert envelope["statuses"]["dependency_healthy"]["value"] is True
        assert envelope["statuses"]["provider_utilization"]["status"] == "unknown"
        assert envelope["statuses"]["provider_utilization"]["value"] is None
        assert envelope["policy"]["source"] == (
            "app.state.operating_envelope_policy" if supplied_policy else "provisional_default"
        )
        assert envelope["current_replicas"] == {
            "value": 2 if supplied_policy else None,
            "source": "supplied_policy" if supplied_policy else "unknown",
        }
        if supplied_policy:
            assert envelope["policy"]["values"] == asdict(policy)
        else:
            assert "current_replicas_unknown" in envelope["reasons"]


async def test_old_workflow_fresh_requeue_has_near_zero_queue_age(monkeypatch):
    async with operations_fixture(monkeypatch) as (http, _, pool, _, db, tenant, _):
        store = WorkflowStore(pool)
        job = await store.submit(tenant, str(uuid4()), {"max_cost": "1"})
        await db.execute(
            "update public.workflow_jobs set created_at=now()-interval '2 hours',"
            "updated_at=now()-interval '2 minutes' where id=$1", job["id"],
        )
        old = await health(http)
        assert old["operating_envelope"]["statuses"]["queue_age_seconds"]["value"] == 120
        assert "workflow_queue_age" in old["warnings"]
        owner = uuid4()
        claimed = await store.claim(owner)
        assert claimed["id"] == job["id"]
        waiting = await store.transition(job["id"], owner, claimed["version"],
                                         "waiting_for_approval", {})
        await store.resume(tenant, job["id"], waiting["version"])
        fresh = await health(http)
        assert fresh["workflows"][0]["count"] == 1
        assert 0 <= fresh["operating_envelope"]["statuses"]["queue_age_seconds"]["value"] < 1
        assert "workflow_queue_age" not in fresh["warnings"]
        assert await db.fetchval(
            "select extract(epoch from now()-created_at) from public.workflow_jobs where id=$1",
            job["id"],
        ) == 7200
        await store.cancel(tenant, job["id"])
        assert (await health(http))["workflows"] == []


async def test_operations_observes_live_provider_leases_and_pending_unknown_holds(monkeypatch):
    async with operations_fixture(monkeypatch) as (http, _, _, ledger, db, tenant, route):
        scope = f"provider:{route.provider_model.provider_id}"
        await db.execute("insert into public.capacity_scopes(scope,capacity) values($1,10)", scope)
        for seconds in (60, 60, -60, 0):
            await db.execute(
                "insert into public.capacity_leases(owner,scope,expires_at) "
                "values($1,$2,now()+$3*interval '1 second')", uuid4(), scope, seconds,
            )
        unknown = await accounting.reserve(ledger, tenant, route, Decimal(1))
        await ledger.dispatch(unknown)
        await ledger.settle(unknown, actual=None)
        pending = await accounting.reserve(ledger, tenant, route, Decimal(1))
        await ledger.dispatch(pending)

        async def interrupted_apply(_reservation):
            raise RuntimeError("Synthetic apply interruption")

        monkeypatch.setattr(ledger, "apply", interrupted_apply)
        await ledger.settle(pending, actual=Decimal("0.5"))
        await db.execute(
            "update public.settlement_outbox set created_at=now()-interval '90 seconds',"
            "next_attempt_at=now()+interval '1 hour' where reservation_id=$1", pending,
        )
        result = await health(http)
        assert result["accounting"] == {
            "unknown_usage": 1, "settlement_backlog": 1, "oldest_settlement_seconds": 90,
        }
        assert {"unknown_usage", "settlement_backlog"} <= set(result["warnings"])
        envelope = result["operating_envelope"]
        assert_advisory(envelope)
        assert "unknown_usage_present" in envelope["reasons"]
        assert envelope["statuses"]["provider_utilization"]["value"] == .5
        assert envelope["statuses"]["oldest_settlement_seconds"]["value"] == 90
        assert await db.fetchval(
            "select reserved_cost from public.client_spend_windows where client_id=$1", tenant,
        ) == 2
