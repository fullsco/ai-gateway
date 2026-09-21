"""Real operations SQL on the dedicated, migrated DB; every fixture write rolls back."""

from contextlib import asynccontextmanager
from dataclasses import asdict
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from gateway.alerts_monitor import _CONDITIONS, evaluate_monitored_rules
from gateway.app import create_app
from gateway.config import Settings
from gateway.maintenance import prune_telemetry
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


async def test_operational_alert_queries_use_real_pending_state(monkeypatch):
    async with operations_fixture(monkeypatch) as (_, _, pool, ledger, db, tenant, route):
        async def observations(kind, threshold=1):
            _, query, bindings = _CONDITIONS[kind]
            assert bindings == ("threshold",)
            return await db.fetch(query, threshold)

        for kind in ("workflow_queue_age", "workflow_approval_backlog",
                     "unknown_usage", "settlement_backlog"):
            await db.execute(
                "insert into public.alert_rules(name,condition_kind,condition,enabled) "
                "values($2,$1,'{\"at_least\":1}'::jsonb,false)", kind, f"Synthetic {uuid4()}",
            )

        store = WorkflowStore(pool)
        job = await store.submit(tenant, str(uuid4()), {"max_cost": "1"})
        await db.execute("update public.workflow_jobs set updated_at=now()-interval '2 minutes' "
                         "where id=$1", job["id"])
        rows = await observations("workflow_queue_age", 60)
        assert len(rows) == 1 and rows[0]["queue_age_seconds"] == 120
        await db.execute("update public.workflow_jobs set attempts=1,updated_at=now() where id=$1",
                         job["id"])
        assert not await observations("workflow_queue_age", 60)
        owner = uuid4()
        claimed = await store.claim(owner)
        await store.transition(job["id"], owner, claimed["version"], "waiting_for_approval", {})
        assert (await observations("workflow_approval_backlog"))[0]["awaiting_approval"] == 1
        await store.cancel(tenant, job["id"])
        assert not await observations("workflow_approval_backlog")

        reservation = await accounting.reserve(ledger, tenant, route, Decimal(1))
        await ledger.dispatch(reservation)
        await ledger.settle(reservation, actual=None)
        assert (await observations("unknown_usage"))[0]["unknown"] == 1
        original = ledger.apply

        async def interrupted(_):
            raise RuntimeError("Synthetic apply interruption")

        monkeypatch.setattr(ledger, "apply", interrupted)
        await ledger.settle(reservation, actual=Decimal("0.25"))
        assert not await observations("unknown_usage")
        rows = await observations("settlement_backlog")
        assert len(rows) == 1 and rows[0]["pending"] == 1
        await original(reservation)
        assert not await observations("settlement_backlog")


async def test_retention_batches_preserve_recent_telemetry_and_unknown_holds(monkeypatch):
    async with operations_fixture(monkeypatch) as (_, _, pool, ledger, db, tenant, route):
        # Refuse to prune any pre-existing eligible rows, even though this rolls back.
        for table, timestamp in (("health_checks", "checked_at"),
                                 ("provider_events", "created_at")):
            assert not await db.fetchval(
                f"select exists(select 1 from public.{table} "
                f"where {timestamp}<now()-interval '30 days')",
            )
        provider = route.provider_model.provider_id
        await db.execute(
            "insert into public.health_checks(provider_id,status,checked_at) "
            "select $1,'healthy',now()-interval '31 days' from generate_series(1,105)", provider,
        )
        await db.execute(
            "insert into public.provider_events(provider_id,event_type,created_at) "
            "select $1,'synthetic',now()-interval '31 days' from generate_series(1,105)", provider,
        )
        await db.execute("insert into public.health_checks(provider_id,status) "
                         "values($1,'healthy')", provider)
        await db.execute("insert into public.provider_events(provider_id,event_type) "
                         "values($1,'synthetic')", provider)
        reservation = await accounting.reserve(ledger, tenant, route, Decimal(1))
        await ledger.dispatch(reservation)
        await ledger.settle(reservation, actual=None)
        assert await prune_telemetry(pool) == {
            "health_checks": "DELETE 100", "provider_events": "DELETE 100",
        }
        assert await prune_telemetry(pool) == {
            "health_checks": "DELETE 5", "provider_events": "DELETE 5",
        }
        for table in ("health_checks", "provider_events"):
            assert await db.fetchval(
                f"select count(*) from public.{table} where provider_id=$1", provider,
            ) == 1
        assert await db.fetchval(
            "select status from public.financial_reservations where id=$1", reservation,
        ) == "unknown"


async def test_queue_alert_is_persisted_deduplicated_and_resolved(monkeypatch):
    async with operations_fixture(monkeypatch) as (_, _, pool, _, db, tenant, _):
        assert not await db.fetchval(
            "select exists(select 1 from public.alert_rules where enabled "
            "and condition_kind is not null)",
        ), "Only this rollback-scoped rule may run in the dedicated fixture"
        rule_id = await db.fetchval(
            "insert into public.alert_rules(name,condition_kind,condition,enabled,scope_id,"
            "cooldown_seconds) values($1,'workflow_queue_age','{\"at_least\":60}'::jsonb,"
            "true,$2,0) returning id", f"Synthetic queue {uuid4()}", str(tenant),
        )
        store = WorkflowStore(pool)
        job = await store.submit(tenant, str(uuid4()), {"max_cost": "1"})
        await db.execute("update public.workflow_jobs set updated_at=now()-interval '2 minutes' "
                         "where id=$1", job["id"])
        assert (await evaluate_monitored_rules(pool)).raised == 1
        assert (await evaluate_monitored_rules(pool)).raised == 1
        rows = await db.fetch(
            "select status,occurrence_count from public.alerts where rule_id=$1", rule_id,
        )
        assert len(rows) == 1 and dict(rows[0]) == {"status": "open", "occurrence_count": 2}
        await store.cancel(tenant, job["id"])
        assert (await evaluate_monitored_rules(pool)).resolved == 1
        assert await db.fetchval(
            "select status from public.alerts where rule_id=$1", rule_id,
        ) == "resolved"


# ── Admin workflow API (gateway/admin/workflows.py) ─────────────────────────
# Operator-authorized, read-only inspection across tenants. The client workflow
# API is tenant-scoped and key-authenticated; this one authenticates with the
# same synthetic admin session the operations health probe uses, so the guard,
# the client-name join and the JSONB decode are all exercised against real rows.
LIST_URL = "/api/admin/v1/workflows"


async def test_admin_workflow_list_requires_admin_and_never_leaks_rows(monkeypatch):
    async with operations_fixture(monkeypatch) as (http, _, pool, _, _, tenant, _):
        for headers in ({}, {"Authorization": "Bearer invalid"}):
            response = await http.get(LIST_URL, headers=headers)
            assert response.status_code == 401
            assert "data" not in response.json()
        # A rejected caller must not have reached the workflow tables at all.
        assert pool.queries == []


async def test_admin_workflow_list_joins_client_name_and_filters(monkeypatch):
    async with operations_fixture(monkeypatch) as (http, _, pool, _, db, tenant, _):
        client_name = await db.fetchval(
            "select name from public.gateway_clients where id=$1", tenant,
        )
        store = WorkflowStore(pool)
        older = await store.submit(tenant, str(uuid4()), {"max_cost": "1"})
        newer = await store.submit(tenant, str(uuid4()), {"max_cost": "2"})
        # created_at defaults to the transaction's now(), so both share a timestamp;
        # age one row to make the created_at-desc order deterministic to assert.
        await db.execute(
            "update public.workflow_jobs set created_at=now()-interval '1 minute' where id=$1",
            older["id"],
        )

        listed = await http.get(LIST_URL, headers=HEADERS)
        assert listed.status_code == 200, listed.text
        data = listed.json()["data"]
        assert [row["id"] for row in data] == [str(newer["id"]), str(older["id"])]
        row = data[0]
        assert row["client_id"] == str(tenant)
        assert row["client_name"] == client_name
        assert row["status"] == "queued"
        # The list is a summary: the input payload and checkpoint stay in the detail view.
        assert "input" not in row and "checkpoint" not in row

        # client_id filter: the tenant's rows come back, an unrelated client sees none.
        mine = await http.get(LIST_URL, params={"client_id": str(tenant)}, headers=HEADERS)
        assert {r["id"] for r in mine.json()["data"]} == {str(older["id"]), str(newer["id"])}
        stranger = await http.get(LIST_URL, params={"client_id": str(uuid4())}, headers=HEADERS)
        assert stranger.json()["data"] == []

        # status filter: a claimed job leaves the queued set and joins the running one.
        claimed = await store.claim(uuid4())
        queued = await http.get(LIST_URL, params={"status": "queued"}, headers=HEADERS)
        assert {r["id"] for r in queued.json()["data"]} == {
            str(older["id"]),
            str(newer["id"]),
        } - {str(claimed["id"])}
        running = await http.get(LIST_URL, params={"status": "running"}, headers=HEADERS)
        assert [r["id"] for r in running.json()["data"]] == [str(claimed["id"])]

        rejected = await http.get(LIST_URL, params={"status": "nonsense"}, headers=HEADERS)
        assert rejected.status_code == 400
        assert rejected.json() == {"error": "invalid_status"}


async def test_admin_workflow_detail_decodes_checkpoint_and_orders_events(monkeypatch):
    async with operations_fixture(monkeypatch) as (http, _, pool, _, db, tenant, _):
        client_name = await db.fetchval(
            "select name from public.gateway_clients where id=$1", tenant,
        )
        store = WorkflowStore(pool)
        payload = {"max_cost": "1", "goal": "settle invoice"}
        job = await store.submit(tenant, str(uuid4()), payload)
        owner = uuid4()
        claimed = await store.claim(owner)
        checkpoint = {"awaiting": "human approval", "step": 3}
        await store.transition(
            job["id"], owner, claimed["version"], "waiting_for_approval", checkpoint,
        )

        detail = await http.get(f"{LIST_URL}/{job['id']}", headers=HEADERS)
        assert detail.status_code == 200, detail.text
        body = detail.json()
        assert body["id"] == str(job["id"])
        assert body["client_name"] == client_name
        assert body["status"] == "waiting_for_approval"
        # JSONB columns arrive decoded, not as strings.
        assert body["input"] == payload
        assert body["checkpoint"] == checkpoint
        # queued(0) -> running -> waiting_for_approval, newest first, capped at 200.
        assert [event["kind"] for event in body["events"]] == [
            "waiting_for_approval", "running", "queued",
        ]
        assert [event["sequence"] for event in body["events"]] == sorted(
            (event["sequence"] for event in body["events"]), reverse=True,
        )

        missing = await http.get(f"{LIST_URL}/{uuid4()}", headers=HEADERS)
        assert missing.status_code == 404
        assert missing.json() == {"error": "workflow_not_found"}


async def test_admin_workflow_api_is_404_when_workflows_disabled(monkeypatch):
    async with operations_fixture(monkeypatch) as (http, app, pool, _, _, tenant, _):
        store = WorkflowStore(pool)
        job = await store.submit(tenant, str(uuid4()), {"max_cost": "1"})
        # The router reads the flag off app.state at request time, so flipping it
        # exercises the disabled branch without standing up a second app/runtime.
        app.state.settings.workflows_enabled = False
        try:
            listed = await http.get(LIST_URL, headers=HEADERS)
            detail = await http.get(f"{LIST_URL}/{job['id']}", headers=HEADERS)
        finally:
            app.state.settings.workflows_enabled = True
        assert listed.status_code == 404 and listed.json() == {"error": "workflows_disabled"}
        assert detail.status_code == 404 and detail.json() == {"error": "workflows_disabled"}
        # With the flag restored the same job is inspectable again.
        assert (await http.get(f"{LIST_URL}/{job['id']}", headers=HEADERS)).status_code == 200
