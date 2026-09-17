from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from gateway.alert_delivery_store import AlertDeliveryStore

EVENT_ID = UUID("d023b39b-e1a8-409c-89c4-08286f578e26")
TOKEN = UUID("812dfabd-4e52-4679-a3dd-206981405bc7")
CLAIM = {"max_attempts": 5, "lease_seconds": 30, "max_age_seconds": 3600}
MIGRATION = Path(__file__).resolve().parents[1] / "migrations/035_alert_delivery.sql"


def sql(pool):
    return " ".join(pool.fetchrow.call_args.args[0].lower().split())


@pytest.mark.asyncio
async def test_schema_ready_requires_columns_types_and_enabled_triggers():
    pool = AsyncMock()
    pool.fetchrow.return_value = {"ready": True}
    await AlertDeliveryStore(pool).require_schema()
    pool.fetchrow.assert_awaited_once()
    query = sql(pool)
    assert "information_schema.columns" in query
    assert "actual.data_type=expected.data_type" in query
    assert "('alert_delivery_outbox','alert_id','bigint')" in query
    assert "('alert_rules','delivery_destination','text')" in query
    assert "('alerts','delivery_sequence','bigint')" in query
    assert "tgenabled in ('o','a')" in query
    assert "count(*)=2" in query
    assert "alert_delivery_sequence','alert_delivery_capture" in query


@pytest.mark.asyncio
@pytest.mark.parametrize("row", [None, {"ready": False}])
async def test_schema_missing_fails_closed(row):
    pool = AsyncMock()
    pool.fetchrow.return_value = row
    with pytest.raises(RuntimeError, match="035"):
        await AlertDeliveryStore(pool).require_schema()


@pytest.mark.asyncio
async def test_claim_returns_complete_snapshot_and_releases_pool():
    now = datetime.now(UTC)
    snapshot = {
        "id": EVENT_ID, "alert_id": 123, "destination": "ops", "sequence": 3,
        "event_type": "provider_failure_rate", "severity": "critical", "status": "resolved",
        "previous_status": "acknowledged", "occurred_at": now, "delivery_status": "pending",
        "attempts": 2, "next_attempt_at": now, "lease_token": TOKEN, "lease_until": now,
        "last_error": "timeout", "last_status": None,
    }
    pool = AsyncMock()
    pool.fetchrow.return_value = snapshot
    result = await AlertDeliveryStore(pool).claim("ops", **CLAIM)
    assert result == snapshot
    assert result is not snapshot
    assert pool.fetchrow.call_args.args[1:] == ("ops", 5, 30.0, 3600.0)
    assert [call[0] for call in pool.mock_calls] == ["fetchrow"]
    query = sql(pool)
    assert query.count("for update skip locked") == 2
    assert "for update skip locked limit 100" in query
    assert "for update skip locked limit 1" in query
    assert query.count("destination=$1 and delivery_status='pending'") == 2
    assert query.count("lease_until is null or lease_until<=now()") == 2
    assert "attempts >= $2" in query
    assert "attempts < $2" in query
    assert "occurred_at<=now()-$4::double precision*interval '1 second'" in query
    assert "occurred_at>now()-$4::double precision*interval '1 second'" in query
    assert "select count(*) from retired" in query
    assert "next_attempt_at<=now()" in query
    assert "attempts=o.attempts+1,lease_token=gen_random_uuid()" in query
    assert "delivery_status='dead',lease_token=null,lease_until=null" in query
    assert "'max_attempts' else 'max_age'" in query
    assert "returning o.*" in query
    assert ";" not in query


@pytest.mark.asyncio
async def test_claim_empty_queue_returns_none():
    pool = AsyncMock()
    pool.fetchrow.return_value = None
    assert await AlertDeliveryStore(pool).claim("ops", **CLAIM) is None
    pool.fetchrow.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["delivered", "pending", "dead"])
@pytest.mark.parametrize("row,expected", [(None, False), ({"id": EVENT_ID}, True)])
async def test_finish_fences_all_outcomes_and_releases_lease(outcome, row, expected):
    pool = AsyncMock()
    pool.fetchrow.return_value = row
    assert await AlertDeliveryStore(pool).finish(
        str(EVENT_ID), str(TOKEN), outcome=outcome, delay_seconds=2.5,
        error="http_status", status_code=503,
    ) is expected
    assert pool.fetchrow.call_args.args[1:] == (
        EVENT_ID, TOKEN, outcome, 2.5, "http_status", 503,
    )
    assert [call[0] for call in pool.mock_calls] == ["fetchrow"]
    query = sql(pool)
    assert "id=$1 and lease_token=$2 and lease_until>now()" in query
    assert "and delivery_status='pending'" in query
    assert "lease_token=null,lease_until=null" in query
    assert "next_attempt_at=case when $3='pending'" in query
    assert "last_error=$5,last_status=$6" in query
    assert "returning id" in query


@pytest.mark.asyncio
async def test_finish_default_arguments():
    pool = AsyncMock()
    pool.fetchrow.return_value = {"id": EVENT_ID}
    assert await AlertDeliveryStore(pool).finish(EVENT_ID, TOKEN, outcome="delivered")
    assert pool.fetchrow.call_args.args[1:] == (EVENT_ID, TOKEN, "delivered", 0.0, None, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("destination", [None, 1, True, "", "a" * 65, "é", "ops\n", "a/b"])
async def test_reject_invalid_destination_before_sql(destination):
    pool = AsyncMock()
    with pytest.raises(ValueError, match="destination"):
        await AlertDeliveryStore(pool).claim(destination, **CLAIM)
    pool.fetchrow.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("destination", ["a", "Z09_-", "a" * 64])
async def test_destination_boundaries(destination):
    pool = AsyncMock()
    pool.fetchrow.return_value = None
    await AlertDeliveryStore(pool).claim(destination, **CLAIM)
    pool.fetchrow.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [True, False, 0, -1, 1.5, "5", None, 2147483648])
async def test_reject_invalid_max_attempts(value):
    pool = AsyncMock()
    with pytest.raises(ValueError, match="max_attempts"):
        await AlertDeliveryStore(pool).claim("ops", **{**CLAIM, "max_attempts": value})
    pool.fetchrow.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("name,maximum", [("lease_seconds", 86400), ("max_age_seconds", 31536000)])
@pytest.mark.parametrize("value", [True, 0, -1, "3", None, float("nan"), float("inf"), 10**400])
async def test_reject_invalid_claim_seconds(name, maximum, value):
    pool = AsyncMock()
    with pytest.raises(ValueError, match=name):
        await AlertDeliveryStore(pool).claim("ops", **{**CLAIM, name: value})
    with pytest.raises(ValueError, match=name):
        await AlertDeliveryStore(pool).claim("ops", **{**CLAIM, name: maximum + 1})
    pool.fetchrow.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("attempts,lease,age", [(1, 0.5, 0.5), (2147483647, 86400, 31536000)])
async def test_claim_numeric_boundaries(attempts, lease, age):
    pool = AsyncMock()
    pool.fetchrow.return_value = None
    await AlertDeliveryStore(pool).claim(
        "ops", max_attempts=attempts, lease_seconds=lease, max_age_seconds=age,
    )
    pool.fetchrow.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("event_id", None), ("event_id", "invalid"), ("event_id", 1),
    ("token", None), ("token", "invalid"), ("token", 1),
    ("outcome", "running"), ("outcome", None), ("outcome", []),
    ("delay_seconds", -1), ("delay_seconds", True), ("delay_seconds", "1"),
    ("delay_seconds", float("nan")), ("delay_seconds", float("inf")),
    ("delay_seconds", 31536001), ("delay_seconds", 10**400),
    ("error", ""), ("error", "x" * 65), ("error", "timeout\n"),
    ("error", "https://secret.invalid"), ("error", "Authorization: token"),
    ("error", []), ("status_code", True), ("status_code", 99),
    ("status_code", 600), ("status_code", 200.0), ("status_code", "200"),
])
async def test_reject_invalid_finish_arguments_before_sql(field, value):
    pool = AsyncMock()
    args = {"event_id": EVENT_ID, "token": TOKEN, "outcome": "pending", field: value}
    with pytest.raises(ValueError):
        await AlertDeliveryStore(pool).finish(**args)
    pool.fetchrow.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [100, 599])
async def test_finish_category_and_status_boundaries(status):
    pool = AsyncMock()
    await AlertDeliveryStore(pool).finish(
        EVENT_ID, TOKEN, outcome="pending", delay_seconds=31536000,
        error="a" * 64, status_code=status,
    )
    pool.fetchrow.assert_awaited_once()


@pytest.mark.asyncio
async def test_database_failures_propagate_without_retry():
    pool = AsyncMock()
    pool.fetchrow.side_effect = RuntimeError("unavailable")
    store = AlertDeliveryStore(pool)
    for operation in (
        store.require_schema(), store.claim("ops", **CLAIM),
        store.finish(EVENT_ID, TOKEN, outcome="pending"),
    ):
        with pytest.raises(RuntimeError, match="unavailable"):
            await operation
    assert pool.fetchrow.await_count == 3


def test_migration_snapshot_permissions_and_no_history_foreign_key():
    query = " ".join(MIGRATION.read_text().lower().split())
    table = query.split("create table public.alert_delivery_outbox (")[1].split("create index")[0]
    assert "id uuid primary key default gen_random_uuid()" in table
    assert "alert_id bigint not null" in table
    assert "references" not in table
    assert "on delete" not in query
    assert "unique (alert_id, sequence)" in table
    assert "delivery_destination text default null" in query
    assert "length(delivery_destination) between 1 and 64" in query
    assert "delivery_destination ~ '^[a-za-z0-9_-]+$'" in query
    assert "enable row level security" in query
    assert "from public, anon, authenticated, service_role" in query
    assert "grant select on public.alert_delivery_outbox to service_role" in query
    assert (
        "grant update (delivery_status, attempts, next_attempt_at, lease_token, lease_until,"
        in query
    )
    assert "grant insert" not in query
    assert "security definer set search_path = pg_catalog" in query
    assert "using (false) with check (false)" in query
    for forbidden in ("metadata", "summary", "title", "scope_id", "recommended_action"):
        assert forbidden not in table
    assert query.startswith("begin;") and query.endswith("commit;")


def test_migration_only_real_transitions_emit_and_upserts_cannot_emit_phantoms():
    query = " ".join(MIGRATION.read_text().lower().split())
    sequence = query.split("create function public.set_alert_delivery_sequence()")[1].split(
        "create function public.capture_alert_delivery()",
    )[0]
    capture = query.split("create function public.capture_alert_delivery()")[1].split(
        "revoke all on function",
    )[0]
    assert "new.delivery_sequence := old.delivery_sequence + 1" in sequence
    assert "new.delivery_sequence := old.delivery_sequence" in sequence
    assert "insert into" not in sequence
    assert "before insert or update of status, delivery_sequence on public.alerts" in query
    assert "after insert or update of status on public.alerts" in query
    assert "if new.status <> 'open' then return new" in capture
    assert "new.status is not distinct from old.status" in capture
    assert "new.status not in ('open','resolved')" in capture
    assert "prior_status := old.status" in capture
    assert "if target_destination is null then return new" in capture
    assert (
        "new.id, target_destination, new.delivery_sequence, new.event_type, new.severity"
        in capture
    )
    assert "new.status, prior_status, now()" in capture
    assert "occurrence_count" not in query
    assert "update public.alerts" not in query
