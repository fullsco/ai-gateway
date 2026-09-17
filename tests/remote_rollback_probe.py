"""Finite version-switch probe using an exported gateway tree and synthetic provider."""

import asyncio
import importlib.util
import json
import os
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4


async def run(root, client, phase):
    import gateway.accounting
    import gateway.workflow_worker
    from gateway.accounting import FinancialLedger
    from gateway.configuration import create_pool
    from gateway.distributed_capacity import CapacityFull, DistributedCapacity
    from gateway.workflow_store import WorkflowStore
    from scripts.seed_dbtest import CREDENTIAL, PROVIDER

    for module in (gateway.accounting, gateway.workflow_worker):
        assert Path(module.__file__).resolve().is_relative_to(root)
    scope = f"qualification:rollback:{client}"
    pool = await create_pool(os.environ["GATEWAY_DATABASE_URL"])
    try:
        store, ledger = WorkflowStore(pool), FinancialLedger(pool)
        capacity = DistributedCapacity(pool, ttl=300)
        if phase == "produce":
            job = await store.submit(client, str(client), {
                "protocol": "openai_chat_completions", "max_steps": 4, "max_cost": "10",
                "request": {"model": "qualification-model", "messages": [], "max_tokens": 16},
            })
            route = SimpleNamespace(
                canonical_model_id="qualification-model",
                credential=SimpleNamespace(credential_id=CREDENTIAL),
                provider_model=SimpleNamespace(provider_id=PROVIDER, route_id=None),
            )
            reservation = await ledger.reserve(
                request_id=str(uuid4()), attempt_number=1, client_id=client, route=route,
                currency="USD", estimated_cost=Decimal(1),
            )
            await ledger.dispatch(reservation)

            async def unavailable_apply(_):
                raise RuntimeError("Synthetic apply outage after committed intent")

            ledger.apply = unavailable_apply
            await ledger.settle(reservation, actual=Decimal("0.25"))
            await capacity.acquire({scope: 1})
            assert (await store.get(client, job["id"]))["status"] == "queued"
            assert await pool.fetchval(
                "select status from public.financial_reservations where id=$1", reservation,
            ) == "settlement_pending"
        elif phase == "consume":
            job_id = await pool.fetchval(
                "select id from public.workflow_jobs where client_id=$1", client,
            )
            assert (await store.get(client, job_id))["status"] == "queued"
            owner = await pool.fetchval(
                "select owner from public.capacity_leases where scope=$1", scope,
            )
            assert owner is not None
            await capacity.renew(owner, 1)
            try:
                await capacity.acquire({scope: 1})
            except CapacityFull:
                pass
            else:
                raise AssertionError("Existing version's lease was ignored")
            await capacity.release(owner)
            reservation = await pool.fetchval(
                "select id from public.financial_reservations where client_id=$1", client,
            )
            await ledger.apply(reservation)
            await ledger.apply(reservation)  # Replay must not apply the price delta twice.
            assert await pool.fetchval(
                "select reserved_cost from public.client_spend_windows where client_id=$1", client,
            ) == Decimal("0.25")
        else:
            raise ValueError("Unsupported phase")
    finally:
        await pool.close()

    if phase == "consume":
        # Reuse the real executor/worker fixture with an in-memory synthetic HTTP provider.
        path = Path(__file__).with_name("remote_execution_probe.py")
        spec = importlib.util.spec_from_file_location("rollback_execution_fixture", path)
        fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture)
        await fixture.run(str(client), "complete")
    print(json.dumps({"phase": phase, "gateway_root": str(root), "pid": os.getpid()}), flush=True)


if __name__ == "__main__":
    root = Path(sys.argv[1]).resolve(strict=True)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.path.insert(0, str(root))
    from scripts.dev_db_check import configure_environment, install_database_bounds, validate_target

    dsn = os.environ["GATEWAY_DATABASE_URL"]
    validate_target(dsn, "kpmxgwflgyzjvitwlfvs")
    configure_environment(dsn)
    from gateway.config import Settings

    assert Path(sys.modules["gateway.config"].__file__).resolve().is_relative_to(root)
    Settings.model_config["env_file"] = None
    install_database_bounds(dsn)
    try:
        asyncio.run(asyncio.wait_for(run(root, UUID(sys.argv[2]), sys.argv[3]), timeout=90))
    except Exception as error:
        # Do not serialize driver errors, arguments, or credentials.
        print(json.dumps({"error_type": type(error).__name__}), flush=True)
        raise SystemExit(1) from None
