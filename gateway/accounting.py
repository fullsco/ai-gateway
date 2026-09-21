"""Transactional financial reservations and replayable, idempotent settlement intent."""

import json
import logging
from decimal import Decimal
from uuid import uuid4

from gateway.metrics import measured
from gateway.quotas import (
    BudgetExceeded,
    ClientSpendingLimitExceeded,
    QuotaUnavailable,
    reserve_budgets,
)
from gateway.workflow_context import active_workflow


class SettlementUnavailable(RuntimeError):
    """Completion cannot be acknowledged; the upstream operation must not be retried."""


class FinancialLedger:
    def __init__(self, pool):
        self.pool = pool

    @measured("durable_reservation")
    async def reserve(self, *, request_id, attempt_number, client_id, route,
                      currency, estimated_cost, estimated_tokens=0, telemetry_request_id=None):
        if (type(attempt_number) is not int or attempt_number < 1
                or type(estimated_tokens) is not int or estimated_tokens < 0):
            raise ValueError("Invalid financial attempt bounds")
        if estimated_cost is not None:
            estimated_cost = Decimal(estimated_cost)
            if not estimated_cost.is_finite() or estimated_cost < 0:
                raise ValueError("Invalid estimated cost")
        reservation_id = uuid4()
        provider_id = route.provider_model.provider_id
        credential_id = route.credential.credential_id
        model_id = route.canonical_model_id
        route_id = route.provider_model.route_id
        try:
            async with self.pool.acquire() as connection, connection.transaction():
                # Lock parent before inserting its FK reference: concurrent KEY SHARE
                # holders must not attempt conflicting lock upgrades after insertion.
                client_limit = await connection.fetchval(
                    "select spending_limit from public.gateway_clients where id=$1 for update",
                    client_id,
                )
                workflow = active_workflow.get()
                if workflow is not None:
                    job_id, owner, version = workflow
                    job = await connection.fetchrow(
                        "select input,reserved_tokens,reserved_cost from public.workflow_jobs "
                        "where id=$1 and client_id=$2 and status='running' and lease_owner=$3 "
                        "and version=$4 for update",
                        job_id, client_id, owner, version,
                    )
                    if job is None:
                        raise QuotaUnavailable("Workflow no longer owns its execution lease")
                    valid = await connection.fetchval(
                        "select lease_expires_at>clock_timestamp() and deadline>clock_timestamp() "
                        "from public.workflow_jobs where id=$1", job_id,
                    )
                    if not valid:
                        raise QuotaUnavailable("Workflow execution lease expired")
                    policy = job["input"]
                    if isinstance(policy, str):
                        policy = json.loads(policy)
                    if estimated_cost is None or currency != policy.get("currency", "USD"):
                        raise BudgetExceeded("Workflow requires pricing in its budget currency")
                    token_limit = policy.get("max_total_tokens", 131072)
                    cost_limit = Decimal(policy.get("max_cost", "10"))
                    if (job["reserved_tokens"] + estimated_tokens > token_limit
                            or job["reserved_cost"] + estimated_cost > cost_limit):
                        raise BudgetExceeded("Workflow cumulative budget exceeded")
                    await connection.execute(
                        "update public.workflow_jobs set reserved_tokens=reserved_tokens+$2,"
                        "reserved_cost=reserved_cost+$3 where id=$1",
                        job_id, estimated_tokens, estimated_cost,
                    )
                # Duplicate attempt identifiers are rejected rather than double-charged.
                row = await connection.fetchrow(
                    "insert into public.financial_reservations "
                    "(id,request_id,attempt_number,client_id,provider_id,credential_id,model_id,"
                    "route_id,currency,estimated_cost,estimated_tokens,telemetry_request_id,"
                    "workflow_id,workflow_owner,workflow_version,client_window) "
                    "values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,"
                    "case when $16::boolean then date_trunc('month',now()) else null end) "
                    "on conflict (request_id,attempt_number) do nothing returning id",
                    reservation_id, request_id, attempt_number, client_id, provider_id,
                    credential_id, model_id, route_id, currency, estimated_cost,
                    estimated_tokens, telemetry_request_id,
                    *(workflow if workflow is not None else (None, None, None)),
                    client_limit is not None,
                )
                if row is None:
                    raise QuotaUnavailable("Duplicate financial attempt identifier")
                # Lock matched budget definitions before recording exact reservation windows.
                budgets = await connection.fetch(
                    "select id, case period when 'daily' then date_trunc('day',now()) "
                    "else date_trunc('month',now()) end as window from public.gateway_budgets "
                    "where enabled and currency=upper($6) and (scope_type='global' or "
                    "(scope_type='client' and scope_id=$1) or "
                    "(scope_type='provider' and scope_id=$2) or "
                    "(scope_type='credential' and scope_id=$3) or "
                    "(scope_type='model' and scope_id=$4) or "
                    "(scope_type='route' and scope_id=$5)) order by id for update",
                    str(client_id), str(provider_id), str(credential_id), model_id,
                    str(route_id) if route_id else None, currency,
                )
                # Unpriced routes (no derivable cost/currency) are governed solely by
                # reserve_budgets below, which refuses them only once an applicable
                # blocking budget is actually exhausted (raising UnpricedRouteBlocked) and
                # otherwise lets them run while the budget still has headroom. The previous
                # checks here refused an unpriced route whenever a blocking budget or a
                # client spending limit merely *existed*, which took every unpriced model
                # offline the moment a $4,000 global cap was enabled - inconsistent with the
                # documented, tested contract in quotas.py (reserve_client_spending is a
                # no-op on unpriced cost; reserve_budgets is the single unpriced arbiter).
                if estimated_cost is not None:
                    result = await connection.fetchval(
                        "select public.reserve_client_spending($1,$2)", client_id, estimated_cost
                    )
                    if result:
                        raise ClientSpendingLimitExceeded("Client spending limit exceeded")
                await reserve_budgets(
                    connection, client_id=client_id, provider_id=provider_id,
                    credential_id=credential_id, model_id=model_id, route_id=route_id,
                    currency=currency, estimated_cost=estimated_cost,
                )
                if estimated_cost is not None:
                    for budget in budgets:
                        await connection.execute(
                            "insert into public.financial_reservation_windows values ($1,$2,$3)",
                            reservation_id, budget["id"], budget["window"],
                        )
        except (BudgetExceeded, QuotaUnavailable):
            raise
        except Exception as exc:
            raise QuotaUnavailable("Durable financial reservation is unavailable") from exc
        return reservation_id

    async def dispatch(self, reservation_id):
        async with self.pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                "select * from public.financial_reservations where id=$1 for update",
                reservation_id,
            )
            if not row or row["status"] != "reserved":
                raise QuotaUnavailable("Financial reservation is not dispatchable")
            if row["workflow_id"]:
                await connection.fetchval(
                    "select id from public.workflow_jobs where id=$1 for update",
                    row["workflow_id"],
                )
                valid = await connection.fetchval(
                    "select status='running' and lease_owner=$2 and version=$3 "
                    "and lease_expires_at>clock_timestamp() and deadline>clock_timestamp() "
                    "from public.workflow_jobs where id=$1", row["workflow_id"],
                    row["workflow_owner"], row["workflow_version"],
                )
                if not valid:
                    raise QuotaUnavailable("Workflow lost ownership before dispatch")
            await connection.execute(
                "update public.financial_reservations set status='dispatched',updated_at=now() "
                "where id=$1", reservation_id,
            )

    @measured("durable_settlement")
    async def settle(self, reservation_id, *, actual):
        if actual is None:
            # Unknown usage is never implicitly zero. Keep the hold for reconciliation.
            await self.pool.execute(
                "update public.financial_reservations set status='unknown',updated_at=now() "
                "where id=$1 and status in ('reserved','dispatched')", reservation_id,
            )
            return
        actual = Decimal(actual)
        if not actual.is_finite() or actual < 0:
            raise ValueError("Invalid actual cost")
        async with self.pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                "select status,actual_cost from public.financial_reservations "
                "where id=$1 for update", reservation_id,
            )
            if row is None:
                raise ValueError("Unknown reservation")
            existing = await connection.fetchval(
                "select actual_cost from public.settlement_outbox where reservation_id=$1",
                reservation_id,
            )
            if existing is not None and existing != actual:
                raise ValueError("Conflicting settlement replay")
            if row["status"] == "settled":
                return
            if row["status"] == "reserved":
                if actual != 0:
                    raise ValueError("Undispatched reservation cannot have a nonzero charge")
                await connection.execute(
                    "update public.financial_reservations set actual_tokens=0 where id=$1",
                    reservation_id,
                )
            await connection.execute(
                "insert into public.settlement_outbox(reservation_id,actual_cost) values ($1,$2) "
                "on conflict do nothing", reservation_id, actual,
            )
            await connection.execute(
                "update public.financial_reservations set status='settlement_pending',"
                "updated_at=now() where id=$1", reservation_id,
            )
        # Intent is durable even if the following apply fails or this process dies.
        try:
            await self.apply(reservation_id)
        except Exception:
            # Intent is committed; the reconciliation worker will replay it.
            return

    async def apply(self, reservation_id):
        async with self.pool.acquire() as connection, connection.transaction():
            client_id = await connection.fetchval(
                "select client_id from public.financial_reservations where id=$1", reservation_id,
            )
            if client_id is None:
                return
            await connection.fetchval(
                "select id from public.gateway_clients where id=$1 for update", client_id,
            )
            row = await connection.fetchrow(
                "select * from public.financial_reservations where id=$1 for update",
                reservation_id,
            )
            intent = await connection.fetchrow(
                "select * from public.settlement_outbox where reservation_id=$1 for update",
                reservation_id,
            )
            if not row or not intent or intent["applied_at"] is not None:
                return
            delta = intent["actual_cost"] - (row["estimated_cost"] or Decimal(0))
            if row.get("workflow_id") is not None:
                usage = await connection.fetchval(
                    "select u.input_tokens+u.output_tokens from public.usage_records u "
                    "join public.request_attempts a on a.id=u.attempt_id "
                    "where a.request_id=$1 and a.attempt_number=$2",
                    row["telemetry_request_id"], row["attempt_number"],
                )
                if row["actual_tokens"] is not None:
                    usage = row["actual_tokens"]
                token_delta = usage - row["estimated_tokens"] if usage is not None else 0
                await connection.execute(
                    "update public.workflow_jobs set reserved_cost=reserved_cost+$2,"
                    "reserved_tokens=reserved_tokens+$3 where id=$1",
                    row["workflow_id"], delta, token_delta,
                )
            windows = await connection.fetch(
                "select budget_id,window_started_at from public.financial_reservation_windows "
                "where reservation_id=$1 order by budget_id", reservation_id,
            )
            for window in windows:
                result = await connection.execute(
                    "update public.budget_usage_windows set reserved_cost=reserved_cost+$3,"
                    "updated_at=now() where budget_id=$1 and window_started_at=$2",
                    window["budget_id"], window["window_started_at"], delta,
                )
                if result != "UPDATE 1":
                    raise SettlementUnavailable("Reserved budget window is missing")
            if row["client_window"] is not None:
                result = await connection.execute(
                    "update public.client_spend_windows set reserved_cost=reserved_cost+$3,"
                    "updated_at=now() where client_id=$1 and window_started_at=$2",
                    row["client_id"], row["client_window"], delta,
                )
                if result != "UPDATE 1":
                    raise SettlementUnavailable("Reserved client window is missing")
            await connection.execute(
                "update public.financial_reservations set status='settled',actual_cost=$2,"
                "updated_at=now() where id=$1", reservation_id, intent["actual_cost"],
            )
            await connection.execute(
                "update public.settlement_outbox set applied_at=now() where reservation_id=$1",
                reservation_id,
            )

    async def reconcile(self, limit=100):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Reconciliation batch must be 1–100")
        rows = await self.pool.fetch(
            "select reservation_id from public.settlement_outbox where applied_at is null "
            "and next_attempt_at<=clock_timestamp() order by next_attempt_at limit $1", limit,
        )
        for row in rows:
            try:
                await self.apply(row["reservation_id"])
            except Exception:
                # One corrupt reservation must not starve unrelated settlement work.
                logging.getLogger("gateway.accounting").error("settlement_apply_deferred")
                await self.pool.execute(
                    "update public.settlement_outbox set attempts=attempts+1,"
                    "next_attempt_at=clock_timestamp()+least(3600,30*power(2,least(attempts,7)))"
                    "*interval '1 second' where reservation_id=$1", row["reservation_id"],
                )
        return len(rows)

    async def recover_abandoned(self):
        async with self.pool.acquire() as connection, connection.transaction():
            rows = await connection.fetch(
                "select id,status,telemetry_request_id,attempt_number "
                "from public.financial_reservations "
                "where status in ('reserved','dispatched','unknown') "
                "and updated_at<now()-interval '1 hour' order by updated_at "
                "for update skip locked limit 100"
            )
            for row in rows:
                actual = await connection.fetchval(
                    "select u.estimated_cost from public.usage_records u "
                    "join public.request_attempts a on a.id=u.attempt_id "
                    "where a.request_id=$1 and a.attempt_number=$2",
                    row["telemetry_request_id"], row["attempt_number"],
                )
                if row["status"] == "reserved" or actual is not None:
                    if row["status"] == "reserved":
                        await connection.execute(
                            "update public.financial_reservations set actual_tokens=0 where id=$1",
                            row["id"],
                        )
                    await connection.execute(
                        "insert into public.settlement_outbox(reservation_id,actual_cost) "
                        "values($1,$2) on conflict do nothing", row["id"], actual or Decimal(0),
                    )
                await connection.execute(
                    "update public.financial_reservations set status=$2,updated_at=now() "
                    "where id=$1",
                    row["id"], "settlement_pending"
                    if row["status"] == "reserved" or actual is not None else "unknown",
                )
        return len(rows)
