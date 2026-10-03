"""Bounded development PostgreSQL pool/transaction baseline, synthetic temporary rows only."""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.benchmark_admission import distribution  # noqa: E402
from scripts.dev_db_check import (  # noqa: E402
    configure_environment,
    load_database_url,
    validate_target,
)


async def run(dsn):
    from gateway.configuration.postgres import create_pool
    from gateway.metrics import current_trace

    pool = await create_pool(dsn)
    cases = []
    try:
        for concurrency in (1, 8, 32, 64, 128):
            semaphore = asyncio.Semaphore(32)
            durations, waits, transactions = [], [], []

            async def worker(index, semaphore=semaphore, durations=durations,
                             waits=waits, transactions=transactions):
                async with semaphore:
                    trace = []
                    token = current_trace.set(trace)
                    start = time.perf_counter()
                    try:
                        async with (
                            pool.acquire(timeout=30) as connection,
                            connection.transaction(),
                        ):
                                # A fresh temporary table belongs to this transaction/backend.
                                await connection.execute(
                                    "create temporary table synthetic_baseline "
                                    "(id integer primary key, value integer not null) "
                                    "on commit drop"
                                )
                                tx = time.perf_counter()
                                await connection.execute(
                                    "insert into synthetic_baseline values ($1,0)", index
                                )
                                await connection.execute(
                                    "update synthetic_baseline set value=value+1 where id=$1", index
                                )
                                assert await connection.fetchval(
                                    "select value from synthetic_baseline where id=$1", index
                                ) == 1
                                transactions.append((time.perf_counter() - tx) * 1000)
                        durations.append((time.perf_counter() - start) * 1000)
                        waits.extend(span["seconds"] * 1000 for span in trace
                                     if span["stage"] == "db_pool_wait")
                    finally:
                        current_trace.reset(token)

            started = time.perf_counter()
            await asyncio.gather(*(worker(index) for index in range(concurrency)))
            elapsed = time.perf_counter() - started
            cases.append({"submitted_concurrency": concurrency, "active_task_limit": 32,
                          "completed": len(durations), "elapsed_seconds": elapsed,
                          "completed_rps": concurrency / elapsed,
                          "pool_wait_ms": distribution(waits),
                          "roundtrip_transaction_ms": distribution(transactions),
                          "total_ms": distribution(durations)})
    finally:
        await pool.close()
    return {"kind": "dev_temporary_postgres_rows", "pool_connections": 5, "cases": cases}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--project-ref", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to("/tmp/opencode") or not output.parent.is_dir() or output.exists():
        parser.error("Choose a fresh output file under an existing /tmp/opencode directory")
    try:
        dsn = load_database_url(args.env_file)
        validate_target(dsn, args.project_ref)
    except ValueError as error:
        parser.error(str(error))
    configure_environment(dsn)
    from gateway.config import Settings

    Settings.model_config["env_file"] = None
    result = asyncio.run(run(dsn))
    result["project_ref"] = args.project_ref
    with output.open("x") as stream:
        json.dump(result, stream, indent=2)
    print(json.dumps({"cases": len(result["cases"]), "evidence": str(output)}))


if __name__ == "__main__":
    main()
