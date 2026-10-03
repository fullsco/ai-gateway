"""Real asyncpg callback correlation, guarded dedicated database only, no application writes."""

import asyncio
import os

import asyncpg
import pytest

from gateway.metrics import measurement_observer, timed
from scripts.dev_db_check import validate_target
from scripts.qualification_tracing import (
    RequestCapture,
    TracedConnection,
    current_capture,
    driver_query_observed,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_query_trace_driver_correlates_commits_errors_and_scope():
    dsn = os.environ.get("GATEWAY_DATABASE_URL")
    if not dsn:
        pytest.skip("Dedicated remote qualification database required")
    validate_target(dsn, "kpmxgwflgyzjvitwlfvs")
    connection = await asyncpg.connect(
        dsn, connection_class=TracedConnection, statement_cache_size=0,
    )
    connection.add_query_logger(driver_query_observed)
    capture = RequestCapture(1)
    token = current_capture.set(capture)
    observer = measurement_observer.set(capture.observe)
    try:
        with timed("shared_circuit_allow"):
            async with connection.transaction():
                assert await connection.fetchval("SELECT $1::int", 42) == 42
        with timed("durable_reservation"), pytest.raises(asyncpg.DivisionByZeroError):
            async with connection.transaction():
                await connection.fetchval("SELECT 1 / $1::int", 0)
        await asyncio.sleep(0)  # Flush the driver's deferred query logger callbacks.
        trace = capture.snapshot()
        assert trace["missing_driver_timings"] == 0
        queries = trace["queries"]
        assert [q["operation"] for q in queries] == [
            "BEGIN", "SELECT", "COMMIT", "BEGIN", "SELECT", "ROLLBACK",
        ]
        assert [q["stage"] for q in queries] == ["shared_circuit_allow"] * 3 + [
            "durable_reservation",
        ] * 3
        assert [q["error"] for q in queries] == [False, False, False, False, True, False]
        for query in queries:
            assert query["driver_callback_count"] == 1
            assert 0 <= query["driver_seconds"] <= query["wall_seconds"]
            assert query["origin"] == "stage_task"
    finally:
        measurement_observer.reset(observer)
        current_capture.reset(token)
        await connection.close()
