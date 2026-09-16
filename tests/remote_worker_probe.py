"""Finite subprocess for dedicated remote crash qualification; no database daemon."""

import asyncio
import json
import os
import sys
from datetime import UTC, datetime


async def run(phase):
    from gateway.config import Settings
    from gateway.configuration import create_pool
    from gateway.workflow_worker import WorkflowWorker

    pool = await create_pool(os.environ["GATEWAY_DATABASE_URL"])
    worker = WorkflowWorker(pool, None, Settings(_env_file=None))

    async def step(job):
        if phase == "completed":
            await worker.store.transition(job["id"], worker.owner, job["version"], "completed",
                                          {"step": 1}, {"synthetic": "durable-result"})
        print(json.dumps({"phase": phase, "job_id": str(job["id"]),
                          "owner": str(worker.owner), "version": job["version"],
                          "observed_at": datetime.now(UTC).isoformat()}), flush=True)
        await asyncio.Event().wait()

    worker._execute = step
    try:
        async with asyncio.timeout(45):
            await worker.run_once()
    finally:
        await pool.close()


async def settlement(reservation_id):
    from decimal import Decimal
    from uuid import UUID

    from gateway.accounting import FinancialLedger
    from gateway.configuration import create_pool

    pool = await create_pool(os.environ["GATEWAY_DATABASE_URL"])
    ledger = FinancialLedger(pool)

    async def crash_point(_):
        print(json.dumps({"phase": "settlement_pending"}), flush=True)
        await asyncio.Event().wait()

    ledger.apply = crash_point
    try:
        async with asyncio.timeout(45):
            await ledger.settle(UUID(reservation_id), actual=Decimal(1))
    finally:
        await pool.close()


async def capacity_probe(scope):
    from gateway.configuration import create_pool
    from gateway.distributed_capacity import CapacityFull, DistributedCapacity

    if not scope.startswith("qualification:"):
        raise ValueError("Synthetic capacity scope required")
    pool = await create_pool(os.environ["GATEWAY_DATABASE_URL"])
    owner = None
    try:
        capacity = DistributedCapacity(pool)
        try:
            owner = await capacity.acquire({scope: 1})
            print(json.dumps({"admitted": True, "owner": str(owner)}), flush=True)
        except CapacityFull:
            print(json.dumps({"admitted": False}), flush=True)
            return
        async with asyncio.timeout(30):
            await asyncio.Event().wait()
    finally:
        if owner:
            await capacity.release(owner)
        await pool.close()


if __name__ == "__main__":
    from scripts.dev_db_check import configure_environment, install_database_bounds, validate_target

    dsn = os.environ["GATEWAY_DATABASE_URL"]
    validate_target(dsn, "kpmxgwflgyzjvitwlfvs")
    configure_environment(dsn)
    from gateway.config import Settings

    Settings.model_config["env_file"] = None
    install_database_bounds(dsn)
    if sys.argv[1] not in {"claimed", "completed", "settlement", "capacity"}:
        raise SystemExit("Invalid probe phase")
    if sys.argv[1] == "capacity":
        asyncio.run(capacity_probe(sys.argv[2]))
    elif sys.argv[1] == "settlement":
        asyncio.run(settlement(sys.argv[2]))
    else:
        asyncio.run(run(sys.argv[1]))
