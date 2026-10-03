import asyncio
import json
import sys
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from gateway.accounting import FinancialLedger
from gateway.workflow_store import WorkflowStore
from tests.test_remote_isolation import dedicated_pool

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.mark.parametrize("phase", ["accepted", "usage"])
async def test_real_executor_worker_death_retains_or_reconstructs_charge(phase):
    async with dedicated_pool() as pool:
        client = uuid4()
        process = None
        job = None
        try:
            assert not await pool.fetchval("select exists(select 1 from public.workflow_jobs)")
            await pool.execute("insert into public.gateway_clients(id,name,spending_limit) "
                               "values($1,'Synthetic execution crash',10)", client)
            store = WorkflowStore(pool)
            job = await store.submit(client, str(uuid4()), {
                "protocol": "openai_chat_completions", "max_steps": 4, "max_cost": "10",
                "request": {"model": "qualification-model", "messages": [], "max_tokens": 16},
            })
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "tests.remote_execution_probe", str(client), phase,
                cwd=Path(__file__).resolve().parents[1], stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            line = await asyncio.wait_for(process.stdout.readline(), 60)
            assert line and json.loads(line)["phase"] == phase
            process.kill()
            await asyncio.wait_for(process.wait(), 5)
            await pool.execute("update public.workflow_jobs "
                               "set lease_expires_at=now()-interval '1 second' "
                               "where id=$1", job["id"])
            await pool.execute("update public.financial_reservations "
                               "set updated_at=now()-interval '2 hours' where client_id=$1", client)
            assert await store.recover() == 1
            assert await store.claim(uuid4()) is None
            ledger = FinancialLedger(pool)
            await ledger.recover_abandoned()
            await ledger.reconcile()
            status = await pool.fetchval("select status from public.financial_reservations "
                                         "where client_id=$1", client)
            assert status == ("unknown" if phase == "accepted" else "settled")
            assert await pool.fetchval("select reserved_cost from public.client_spend_windows "
                                       "where client_id=$1", client) == Decimal("0.25")
            assert await pool.fetchval("select count(*) from public.request_attempts a "
                "join public.request_logs r on r.id=a.request_id where r.client_id=$1", client) == 1
        finally:
            if process is not None and process.returncode is None:
                process.kill()
                await asyncio.wait_for(process.wait(), 5)
            await pool.execute("delete from public.settlement_outbox where reservation_id in "
                               "(select id from public.financial_reservations where client_id=$1)",
                               client)
            await pool.execute("delete from public.financial_reservations where client_id=$1",
                               client)
            if job:
                await pool.execute("delete from public.workflow_jobs where id=$1", job["id"])
            await pool.execute("delete from public.request_logs where client_id=$1", client)
            await pool.execute("delete from public.gateway_clients where id=$1", client)
