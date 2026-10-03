"""Small dedicated-remote accounting contention experiment; never a provider/RPS claim."""

import argparse
import asyncio
import json
import sys
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.benchmark_admission import distribution  # noqa: E402
from scripts.dev_db_check import (  # noqa: E402
    configure_environment,
    install_database_bounds,
    load_database_url,
    validate_target,
)
from scripts.seed_dbtest import CREDENTIAL, PROVIDER  # noqa: E402


async def run(dsn):
    from gateway.accounting import FinancialLedger
    from gateway.configuration import create_pool
    from gateway.metrics import current_trace

    pool = await create_pool(dsn)
    client = uuid4()
    ledger = FinancialLedger(pool)
    cases = []
    try:
        await pool.execute("insert into public.gateway_clients(id,name,spending_limit) "
                           "values($1,'Synthetic accounting load',100)", client)
        route = SimpleNamespace(canonical_model_id="qualification-model",
            credential=SimpleNamespace(credential_id=CREDENTIAL),
            provider_model=SimpleNamespace(provider_id=PROVIDER, route_id=None))
        for concurrency in (1, 2):
            durations, stages = [], []

            async def attempt(durations=durations, stages=stages):
                spans = []
                token = current_trace.set(spans)
                started = perf_counter()
                try:
                    reservation = await ledger.reserve(request_id=str(uuid4()), attempt_number=1,
                        client_id=client, route=route, currency="USD", estimated_cost=Decimal(1))
                    await ledger.dispatch(reservation)
                    await ledger.settle(reservation, actual=Decimal("0.25"))
                    durations.append((perf_counter() - started) * 1000)
                    stages.extend(spans)
                finally:
                    current_trace.reset(token)

            start = perf_counter()
            for _ in range(2):
                await asyncio.wait_for(asyncio.gather(*(attempt() for _ in range(concurrency))), 90)
            elapsed = perf_counter() - start
            cases.append({"concurrency": concurrency, "completed": len(durations),
                "elapsed_seconds": elapsed, "completed_per_second": len(durations) / elapsed,
                "latency_ms": distribution(durations),
                "stages_ms": {stage: distribution([span["seconds"] * 1000 for span in stages
                                                    if span["stage"] == stage])
                              for stage in ("db_pool_wait", "durable_reservation",
                                            "durable_settlement")}})
        charged = await pool.fetchval("select reserved_cost from public.client_spend_windows "
                                      "where client_id=$1", client)
        assert charged == Decimal("1.50")
        assert await pool.fetchval("select count(*) from public.financial_reservations "
                                   "where client_id=$1 and status='settled'", client) == 6
        plans = await pool.fetchval(
            "explain (format json) select reservation_id from public.settlement_outbox "
            "where applied_at is null and next_attempt_at<=clock_timestamp() "
            "order by next_attempt_at limit 100"
        )
        return {"kind": "dedicated_remote_accounting", "pool_connections": 2, "cases": cases,
                "charged_total": str(charged), "settlement_query_plan": json.loads(plans),
                "limitations": ["six synthetic attempts; not a tail-latency SLO",
                                "network RTT included", "no provider calls", "not a soak test"]}
    finally:
        await pool.execute("delete from public.settlement_outbox where reservation_id in "
                           "(select id from public.financial_reservations where client_id=$1)",
                           client)
        await pool.execute("delete from public.financial_reservations where client_id=$1", client)
        await pool.execute("delete from public.gateway_clients where id=$1", client)
        await pool.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env.dbtest")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    target = args.output.resolve()
    if not target.is_relative_to("/tmp/opencode") or not target.parent.is_dir() or target.exists():
        parser.error("Choose a fresh output under /tmp/opencode")
    dsn = load_database_url(args.env_file)
    validate_target(dsn, "kpmxgwflgyzjvitwlfvs")
    configure_environment(dsn)
    install_database_bounds(dsn, max_connections=2)
    from gateway.config import Settings

    Settings.model_config["env_file"] = None
    result = asyncio.run(run(dsn))
    with target.open("x") as stream:
        json.dump(result, stream, indent=2)
    print(json.dumps({"completed": 6, "evidence": str(target)}))


if __name__ == "__main__":
    main()
