"""Bounded retention for operational data, preserving unresolved financial evidence."""

import asyncio
import logging
from time import monotonic

from gateway.logging import log_event

logger = logging.getLogger("gateway.maintenance")


async def maintenance_loop(pool, settings=None, *, interval=5):
    """Always expire artifacts; prune telemetry only when explicitly enabled.

    No financial records, workflow jobs, or workflow events are retention targets.
    Each cleanup has its own deadline, including pool acquisition/schema checks.
    Cancellation belongs to the accounting owner. No cleanup batches overlap.
    """
    timeout = settings.maintenance_timeout_seconds if settings is not None else 5
    telemetry_enabled = settings is not None and settings.maintenance_enabled
    next_telemetry = 0
    while True:
        try:
            async with asyncio.timeout(timeout):
                if await pool.fetchval("select to_regclass('public.workflow_artifacts')"):
                    await collect_expired_artifacts(pool)
        except Exception as exc:
            log_event(logger, logging.WARNING, "maintenance_failed",
                      error_type=type(exc).__name__)
        if telemetry_enabled and monotonic() >= next_telemetry:
            try:
                async with asyncio.timeout(timeout):
                    await prune_telemetry(pool, settings.telemetry_retention_days)
            except Exception as exc:
                log_event(logger, logging.WARNING, "maintenance_failed",
                          error_type=type(exc).__name__)
            next_telemetry = monotonic() + settings.maintenance_interval_seconds
        # Sleep after completion/failure: no overlapping batches or catch-up storms.
        await asyncio.sleep(interval)


async def collect_expired_artifacts(pool):
    return await pool.execute(
        "delete from public.workflow_artifacts where id in "
        "(select id from public.workflow_artifacts where expires_at<=now() "
        "order by expires_at limit 100 for update skip locked)"
    )


async def prune_telemetry(pool, retention_days=30):
    if type(retention_days) is not int or not 7 <= retention_days <= 365:
        raise ValueError("Telemetry retention must be 7–365 days")
    results = {}
    for table, timestamp in (("health_checks", "checked_at"), ("provider_events", "created_at")):
        results[table] = await pool.execute(
            f"delete from public.{table} where id in "
            f"(select id from public.{table} where {timestamp}<now()-$1*interval '1 day' "
            f"order by {timestamp} limit 100 for update skip locked)", retention_days,
        )
    return results
