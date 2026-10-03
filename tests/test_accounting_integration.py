import os
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from gateway.accounting import FinancialLedger
from gateway.configuration import create_pool
from gateway.quotas import BudgetExceeded, QuotaUnavailable, UnpricedRouteBlocked
from gateway.workflow_context import active_workflow
from gateway.workflow_store import WorkflowStore

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


class TransactionPool:
    def __init__(self, connection):
        self.connection = connection

    def __getattr__(self, name):
        return getattr(self.connection, name)

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


@asynccontextmanager
async def fixture():
    dsn = os.environ.get("GATEWAY_DATABASE_URL")
    if not dsn:
        pytest.skip("Development database required")
    pool = await create_pool(dsn)
    try:
        async with pool.acquire() as connection:
            transaction = connection.transaction()
            await transaction.start()
            try:
                await connection.execute("set local lock_timeout='3s'")
                await connection.execute("set local statement_timeout='10s'")
                await connection.execute("set local idle_in_transaction_session_timeout='30s'")
                # Claim/recovery tests must never see pre-existing jobs or financial holds.
                # Fail before fixture writes if the shared dev service has adopted these tables.
                for table in ("financial_reservations", "workflow_jobs", "capacity_leases"):
                    if await connection.fetchval("select to_regclass($1)", f"public.{table}"):
                        occupied = await connection.fetchval(
                            f"select exists(select 1 from public.{table})"
                        )
                        if occupied:
                            pytest.fail(
                                "Dev qualification tables contain data; use an isolated clone"
                            )
                if not await connection.fetchval(
                    "select to_regclass('public.financial_reservations')"
                ):
                    migrations = (Path(__file__).parents[1] / "migrations").glob("*.sql")
                    for migration in sorted(migrations):
                        if int(migration.name[:3]) >= 26:
                            await connection.execute(migration.read_text())
                client = await connection.fetchval(
                    "insert into public.gateway_clients(name,spending_limit) values ($1,10) "
                    "returning id", f"synthetic-{uuid4()}",
                )
                provider = await connection.fetchval(
                    "insert into public.providers(name,provider_type,protocol,base_url) "
                    "values ($1,'openai_compatible','openai_chat_completions',"
                    "'http://127.0.0.1') returning id", f"synthetic-{uuid4()}",
                )
                credential = await connection.fetchval(
                    "insert into public.provider_credentials(provider_id,name,secret_version,"
                    "secret_nonce,secret_ciphertext) values ($1,'synthetic',1,'fake','fake') "
                    "returning id", provider,
                )
                route = SimpleNamespace(
                    canonical_model_id="synthetic",
                    credential=SimpleNamespace(credential_id=credential),
                    provider_model=SimpleNamespace(provider_id=provider, route_id=None),
                )
                yield FinancialLedger(TransactionPool(connection)), connection, client, route
            finally:
                await transaction.rollback()
    finally:
        await pool.close()


async def reserve(ledger, client, route, cost=Decimal(4)):
    return await ledger.reserve(request_id=str(uuid4()), attempt_number=1, client_id=client,
                                route=route, currency="USD", estimated_cost=cost)


@pytest.mark.parametrize("client_limit", [None, Decimal(0), Decimal(10)])
async def test_reservation_client_window_matches_transaction_month_only_when_limited(client_limit):
    async with fixture() as (ledger, db, client, route):
        await db.execute(
            "update public.gateway_clients set spending_limit=$2 where id=$1", client, client_limit,
        )
        reservation = await reserve(ledger, client, route, cost=Decimal(0))
        row = await db.fetchrow(
            "select client_window,workflow_id,workflow_owner,workflow_version,"
            "date_trunc('month',now()) as month from public.financial_reservations where id=$1",
            reservation,
        )
        assert row["client_window"] == (row["month"] if client_limit is not None else None)
        assert row["workflow_id"] is None
        assert row["workflow_owner"] is None
        assert row["workflow_version"] is None
        windows = await db.fetch(
            "select window_started_at from public.client_spend_windows where client_id=$1", client,
        )
        assert [window["window_started_at"] for window in windows] == (
            [row["month"]] if client_limit is not None else []
        )


async def test_workflow_reservation_records_ownership_and_duplicate_rolls_back_charges():
    async with fixture() as (ledger, db, client, route):
        store = WorkflowStore(TransactionPool(db))
        job = await store.submit(client, "synthetic", {"max_cost": "10"})
        owner = uuid4()
        claimed = await store.claim(owner)
        assert claimed["id"] == job["id"]
        request_id = str(uuid4())
        token = active_workflow.set((job["id"], owner, claimed["version"]))
        try:
            reservation = await ledger.reserve(
                request_id=request_id, attempt_number=1, client_id=client, route=route,
                currency="USD", estimated_cost=Decimal(4), estimated_tokens=100,
            )
            row = await db.fetchrow(
                "select workflow_id,workflow_owner,workflow_version "
                "from public.financial_reservations where id=$1", reservation,
            )
            assert tuple(row) == (job["id"], owner, claimed["version"])
            with pytest.raises(QuotaUnavailable, match="Duplicate financial attempt identifier"):
                await ledger.reserve(
                    request_id=request_id, attempt_number=1, client_id=client, route=route,
                    currency="USD", estimated_cost=Decimal(4), estimated_tokens=100,
                )
            assert await db.fetchval(
                "select count(*) from public.financial_reservations where client_id=$1", client,
            ) == 1
            assert await db.fetchval(
                "select reserved_cost from public.client_spend_windows where client_id=$1", client,
            ) == 4
            usage = await db.fetchrow(
                "select reserved_cost,reserved_tokens from public.workflow_jobs where id=$1",
                job["id"],
            )
            assert tuple(usage) == (Decimal(4), 100)
        finally:
            active_workflow.reset(token)


async def test_ledger_settlement_replay_is_idempotent_and_unknown_usage_retains_hold():
    async with fixture() as (ledger, db, client, route):
        reservation = await reserve(ledger, client, route)
        await ledger.dispatch(reservation)
        await ledger.settle(reservation, actual=None)
        assert await db.fetchval("select reserved_cost from public.client_spend_windows "
                                 "where client_id=$1", client) == 4
        assert await db.fetchval("select status from public.financial_reservations where id=$1",
                                 reservation) == "unknown"
        await ledger.settle(reservation, actual=Decimal(1))
        await ledger.settle(reservation, actual=Decimal(1))
        assert await db.fetchval("select reserved_cost from public.client_spend_windows "
                                 "where client_id=$1", client) == 1
        with pytest.raises(ValueError, match="Conflicting"):
            await ledger.settle(reservation, actual=Decimal(2))


async def test_pending_intent_survives_apply_failure_and_reconciles(monkeypatch):
    async with fixture() as (ledger, db, client, route):
        reservation = await reserve(ledger, client, route)
        await ledger.dispatch(reservation)
        original = ledger.apply

        async def fail(_):
            raise RuntimeError("simulated worker death")

        monkeypatch.setattr(ledger, "apply", fail)
        await ledger.settle(reservation, actual=Decimal(2))
        assert await db.fetchval("select status from public.financial_reservations where id=$1",
                                 reservation) == "settlement_pending"
        monkeypatch.setattr(ledger, "apply", original)
        await ledger.reconcile()
        assert await db.fetchval("select reserved_cost from public.client_spend_windows "
                                 "where client_id=$1", client) == 2


async def test_failed_reservation_rolls_back_client_charge_and_ledger_row():
    async with fixture() as (ledger, db, client, route):
        with pytest.raises(BudgetExceeded):
            await reserve(ledger, client, route, cost=Decimal(11))
        assert await db.fetchval("select count(*) from public.financial_reservations "
                                 "where client_id=$1", client) == 0
        assert await db.fetchval("select count(*) from public.client_spend_windows "
                                 "where client_id=$1", client) == 0


async def test_recovery_refunds_only_undispatched_holds_and_retains_unknown_cost():
    async with fixture() as (ledger, db, client, route):
        first = await reserve(ledger, client, route)
        second = await reserve(ledger, client, route)
        await ledger.dispatch(second)
        await db.execute(
            "update public.financial_reservations set updated_at=now()-interval '2 hours' "
            "where id=any($1::uuid[])", [first, second],
        )
        assert await ledger.recover_abandoned() == 2
        await ledger.reconcile()
        assert await db.fetchval("select status from public.financial_reservations where id=$1",
                                 first) == "settled"
        assert await db.fetchval("select status from public.financial_reservations where id=$1",
                                 second) == "unknown"
        assert await db.fetchval("select reserved_cost from public.client_spend_windows "
                                 "where client_id=$1", client) == 4


async def test_budget_refusal_rolls_back_prior_client_reservation():
    async with fixture() as (ledger, db, client, route):
        await db.execute(
            "insert into public.gateway_budgets(name,scope_type,scope_id,currency,"
            "limit_amount,enforcement) values($1,'client',$2,'USD',1,'block')",
            f"synthetic-{uuid4()}", str(client),
        )
        with pytest.raises(BudgetExceeded):
            await reserve(ledger, client, route)
        assert await db.fetchval(
            "select count(*) from public.client_spend_windows where client_id=$1", client,
        ) == 0
        assert await db.fetchval(
            "select count(*) from public.financial_reservations where client_id=$1", client,
        ) == 0


async def test_settlement_uses_reserved_window_and_budget_even_after_policy_changes():
    async with fixture() as (ledger, db, client, route):
        budget = await db.fetchval(
            "insert into public.gateway_budgets(name,scope_type,scope_id,currency,"
            "limit_amount,enforcement) values($1,'client',$2,'USD',10,'block') returning id",
            f"synthetic-{uuid4()}", str(client),
        )
        reservation = await reserve(ledger, client, route)
        await ledger.dispatch(reservation)
        old_window = await db.fetchval("select date_trunc('month',now())-interval '1 month'")
        await db.execute("update public.budget_usage_windows set window_started_at=$2 "
                         "where budget_id=$1", budget, old_window)
        await db.execute("update public.financial_reservation_windows set window_started_at=$2 "
                         "where reservation_id=$1", reservation, old_window)
        await db.execute("update public.gateway_budgets set enabled=false where id=$1", budget)
        await ledger.settle(reservation, actual=Decimal(1))
        actual = await db.fetchval(
            "select reserved_cost from public.budget_usage_windows "
            "where budget_id=$1 and window_started_at=$2", budget, old_window,
        )
        assert actual == 1


async def test_poisoned_oldest_outbox_entry_is_backed_off_so_later_work_can_progress(monkeypatch):
    async with fixture() as (ledger, db, client, route):
        first = await reserve(ledger, client, route)
        second = await reserve(ledger, client, route)
        await ledger.dispatch(first)
        await ledger.dispatch(second)
        apply = ledger.apply

        async def fail(_):
            raise RuntimeError("synthetic corruption")

        monkeypatch.setattr(ledger, "apply", fail)
        await ledger.settle(first, actual=Decimal(1))
        await ledger.settle(second, actual=Decimal(1))
        await db.execute("update public.settlement_outbox "
                         "set next_attempt_at=now()-interval '1 minute' "
                         "where reservation_id=$1", first)
        assert await ledger.reconcile(limit=1) == 1
        monkeypatch.setattr(ledger, "apply", apply)
        assert await ledger.reconcile(limit=1) == 1
        assert await db.fetchval("select status from public.financial_reservations where id=$1",
                                 second) == "settled"
        assert await db.fetchval(
            "select attempts from public.settlement_outbox where reservation_id=$1", first,
        ) == 1


async def test_unpriced_route_runs_under_a_blocking_budget_with_headroom():
    """Incident regression: an enabled global block budget with headroom must not
    take unpriced routes offline.

    The durable path previously refused an unpriced route whenever a blocking
    budget merely *existed*, which broke every unpriced model the moment a $4,000
    global cap was enabled. The correct contract (quotas.reserve_budgets) lets it
    run while the budget still has headroom.
    """
    async with fixture() as (ledger, db, client, route):
        # Fixture client carries spending_limit=10; an unpriced request must still run
        # (neither the client-limit guard nor the block-budget existence guard may refuse it).
        await db.execute(
            "insert into public.gateway_budgets(name,scope_type,scope_id,currency,"
            "limit_amount,enforcement) values($1,'global',null,'USD',4000,'block')",
            f"synthetic-{uuid4()}",
        )
        reservation = await ledger.reserve(
            request_id=str(uuid4()), attempt_number=1, client_id=client, route=route,
            currency=None, estimated_cost=None,
        )
        assert await db.fetchval(
            "select status from public.financial_reservations where id=$1", reservation,
        ) == "reserved"


async def test_unpriced_route_refused_once_a_blocking_budget_is_exhausted():
    """The cap must still be un-evadable: once the global block budget is at its
    limit, an unpriced route (which can reserve nothing) is refused with the
    specific UnpricedRouteBlocked signal so the executor excludes it.
    """
    async with fixture() as (ledger, db, client, route):
        budget = await db.fetchval(
            "insert into public.gateway_budgets(name,scope_type,scope_id,currency,"
            "limit_amount,enforcement) values($1,'global',null,'USD',4000,'block') returning id",
            f"synthetic-{uuid4()}",
        )
        await db.execute(
            "insert into public.budget_usage_windows(budget_id,window_started_at,reserved_cost) "
            "values($1,date_trunc('month',now()),4000)", budget,
        )
        with pytest.raises(UnpricedRouteBlocked):
            await ledger.reserve(
                request_id=str(uuid4()), attempt_number=1, client_id=client, route=route,
                currency=None, estimated_cost=None,
            )
        assert await db.fetchval(
            "select count(*) from public.financial_reservations where client_id=$1", client,
        ) == 0
