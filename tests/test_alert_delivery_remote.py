"""Real 035 delivery trigger, claim and fencing on the dedicated migrated database.

Rollback-scoped: every write happens inside the fixture transaction and is undone,
except schema application which uses committed DDL on the dedicated project only.
"""

import os
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from gateway.alert_delivery_store import AlertDeliveryStore
from gateway.configuration import create_pool
from scripts.dev_db_check import validate_target
from tests.test_accounting_integration import TransactionPool

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
MIGRATIONS = [
    Path(__file__).resolve().parents[1] / "migrations/035_alert_delivery.sql",
    Path(__file__).resolve().parents[1] / "migrations/036_alert_delivery_sequence_fix.sql",
]


@asynccontextmanager
async def dedicated_pool():
    dsn = os.environ.get("GATEWAY_DATABASE_URL")
    if not dsn:
        pytest.skip("Dedicated remote qualification database required")
    validate_target(dsn, "kpmxgwflgyzjvitwlfvs")
    pool = await create_pool(dsn)
    try:
        if pool.get_max_size() != 2:
            pytest.fail("Use the dedicated runner --isolated option for two connections")
        yield pool
    finally:
        await pool.close()


@asynccontextmanager
async def alert_rule(pool, delivery_destination=None):
    async with pool.acquire() as db:
        transaction = db.transaction()
        await transaction.start()
        try:
            rule_id = await db.fetchval(
                "insert into public.alert_rules(name,event_type,severity,enabled,"
                "delivery_destination) values($1,'synthetic_event','warning',true,$2) "
                "returning id", f"Synthetic delivery {uuid4()}", delivery_destination,
            )
            yield TransactionPool(db), rule_id
        finally:
            await transaction.rollback()


async def open_alert(db, rule_id, severity="warning"):
    return await db.fetchval(
        "insert into public.alerts(rule_id,dedup_key,severity,event_type,title) "
        "values($1,$2,$3,'synthetic_event','Synthetic') returning id",
        rule_id, f"dedup-{uuid4()}", severity,
    )


async def test_migration_035_and_036_apply_on_dedicated_project():
    async with dedicated_pool() as pool:
        if await pool.fetchval("select to_regclass('public.alert_delivery_outbox')"):
            await AlertDeliveryStore(pool).require_schema()
            return
        for migration in MIGRATIONS:
            await pool.execute(migration.read_text())
        await AlertDeliveryStore(pool).require_schema()


async def test_subscribed_rule_captures_open_and_upsert_does_not():
    async with dedicated_pool() as pool, alert_rule(pool, "default") as (db, rule_id):
        for _ in range(3):
            await db.execute(
                "insert into public.alerts(rule_id,dedup_key,severity,event_type,title) "
                "values($1,$2,'warning','synthetic_event','Synthetic') on conflict (dedup_key) "
                "where status in ('open','acknowledged') do update set "
                "occurrence_count=alerts.occurrence_count+1,last_seen_at=now()",
                rule_id, f"dedup-{uuid4()}",
            )
        await open_alert(db, rule_id)
        events = await db.fetch(
            "select sequence,status from public.alert_delivery_outbox "
            "where alert_id=(select max(id) from public.alerts) order by sequence",
        )
        assert [dict(event) for event in events] == [{"sequence": 1, "status": "open"}]


async def test_open_to_resolved_captures_transition_and_ack_does_not():
    async with dedicated_pool() as pool, alert_rule(pool, "default") as (db, rule_id):
        connection = db.connection
        alert_id = await open_alert(connection, rule_id)
        assert await connection.fetchval(
            "select delivery_sequence from public.alerts where id=$1", alert_id,
        ) == 1
        await connection.execute(
            "update public.alerts set status='acknowledged' where id=$1", alert_id)
        await connection.execute(
            "update public.alerts set status='resolved' where id=$1", alert_id)
        events = await connection.fetch(
            "select sequence,status,previous_status from public.alert_delivery_outbox "
            "where alert_id=$1 order by sequence", alert_id,
        )
        assert [(event["sequence"], event["status"], event["previous_status"])
                for event in events] == [
            (1, "open", None), (2, "resolved", "acknowledged"),
        ]


async def test_claim_fencing_and_exhaustion_on_real_rows():
    async with dedicated_pool() as pool, alert_rule(pool, "default") as (db, rule_id):
        connection = db.connection
        alert_id = await open_alert(connection, rule_id, "critical")
        store = AlertDeliveryStore(TransactionPool(connection))
        event = await store.claim("default", max_attempts=2, lease_seconds=30,
                                  max_age_seconds=3600)
        assert event is not None and event["alert_id"] == alert_id
        assert event["attempts"] == 1 and event["status"] == "open"
        assert await store.finish(event["id"], event["lease_token"],
                                  outcome="delivered") is True
        assert await store.claim("default", max_attempts=2, lease_seconds=30,
                                 max_age_seconds=3600) is None
        await connection.execute(
            "update public.alert_delivery_outbox set delivery_status='pending',attempts=0,"
            "next_attempt_at=now() where id=$1", event["id"],
        )
        event = await store.claim("default", max_attempts=2, lease_seconds=30,
                                  max_age_seconds=3600)
        assert event["attempts"] == 1
        assert await store.finish(event["id"], event["lease_token"], outcome="pending",
                                  delay_seconds=1, error="http_transient") is True
        assert await store.claim("default", max_attempts=2, lease_seconds=30,
                                 max_age_seconds=3600) is None
        await connection.execute(
            "update public.alert_delivery_outbox set next_attempt_at=now() where id=$1",
            event["id"],
        )
        retry = await store.claim("default", max_attempts=2, lease_seconds=30,
                                  max_age_seconds=3600)
        assert retry is not None and retry["attempts"] == 2
        assert await store.finish(retry["id"], retry["lease_token"], outcome="pending",
                                  delay_seconds=1, error="http_transient") is True
        await connection.execute(
            "update public.alert_delivery_outbox set next_attempt_at=now() where id=$1",
            retry["id"],
        )
        assert await store.claim("default", max_attempts=2, lease_seconds=30,
                                 max_age_seconds=3600) is None
        final = await connection.fetchrow(
            "select delivery_status,last_error from public.alert_delivery_outbox where id=$1",
            event["id"],
        )
        assert final["delivery_status"] == "dead" and final["last_error"] == "max_attempts"


async def test_wrong_token_cannot_finish_and_lease_expiry_allows_reclaim():
    async with dedicated_pool() as pool, alert_rule(pool, "default") as (db, rule_id):
        connection = db.connection
        await open_alert(connection, rule_id)
        store = AlertDeliveryStore(TransactionPool(connection))
        event = await store.claim("default", max_attempts=3, lease_seconds=30,
                                  max_age_seconds=3600)
        wrong = await store.finish(event["id"], uuid4(), outcome="delivered")
        assert wrong is False
        still = await connection.fetchval(
            "select delivery_status from public.alert_delivery_outbox where id=$1", event["id"],
        )
        assert still == "pending"
        await connection.execute(
            "update public.alert_delivery_outbox set lease_until=now()-interval '1 second' "
            "where id=$1", event["id"],
        )
        reclaimed = await store.claim("default", max_attempts=3, lease_seconds=30,
                                      max_age_seconds=3600)
        assert reclaimed["id"] == event["id"] and reclaimed["attempts"] == 2
        assert reclaimed["lease_token"] != event["lease_token"]


async def test_unsubscribed_rule_captures_nothing_and_bad_destination_rejected():
    async with dedicated_pool() as pool, alert_rule(pool) as (db, rule_id):
        connection = db.connection
        await open_alert(connection, rule_id)
        assert not await connection.fetchval(
            "select exists(select 1 from public.alert_delivery_outbox)",
        )
    async with dedicated_pool() as pool, pool.acquire() as db:
        transaction = db.transaction()
        await transaction.start()
        try:
            with pytest.raises(asyncpg.CheckViolationError):
                await db.execute(
                    "insert into public.alert_rules(name,event_type,enabled,"
                    "delivery_destination) values($1,'synthetic_event',true,'bad name')",
                    f"Synthetic bad destination {uuid4()}",
                )
        finally:
            await transaction.rollback()
