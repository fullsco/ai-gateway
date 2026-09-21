"""Two fresh gateway processes across a version switch, isolated DB only."""

import asyncio
import io
import json
import os
import subprocess
import sys
import tarfile
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest

from tests.test_remote_isolation import dedicated_pool

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
ROOT = Path(__file__).resolve().parents[1]
BASELINE = "0bac80ee1db12dda4cb6103df00b54ad824958e2"
CURRENT = "9ebe1821529bc8607fd754ba736e2bfe601e9e73"


def export_gateway(commit, destination):
    archive = subprocess.check_output(
        ["git", "archive", commit, "gateway"], cwd=ROOT, timeout=15,
    )
    with tarfile.open(fileobj=io.BytesIO(archive)) as tree:
        for member in tree:
            path = Path(member.name)
            assert not path.is_absolute() and ".." not in path.parts
            assert path.parts[0] == "gateway"
            if member.isdir():
                continue
            assert member.isfile() and path.suffix == ".py"
            target = destination / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(tree.extractfile(member).read())


async def probe(source, client, phase):
    env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "GATEWAY_DATABASE_URL")
           if key in os.environ}
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-I", str(ROOT / "tests/remote_rollback_probe.py"),
        str(source), str(client), phase, cwd=source, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), 100)
        if process.returncode:
            # The child emits an error class, never SQL/DSNs or provider content.
            try:
                error_type = json.loads(output).get("error_type", "UnknownError")
            except (ValueError, AttributeError):
                error_type = "InvalidProbeOutput"
            assert isinstance(error_type, str) and error_type.isidentifier()
            pytest.fail(f"Version-switch {phase} probe failed: {error_type}")
        result = json.loads(output)
        assert result["phase"] == phase and result["gateway_root"] == str(source)
        return result
    finally:
        if process.returncode is None:
            process.kill()
            await asyncio.wait_for(process.wait(), 5)


@pytest.mark.parametrize("rollback", [True, False], ids=["rollback", "forward"])
async def test_version_switch_drains_queued_work_and_pending_settlement(rollback):
    with TemporaryDirectory(prefix="gateway-rollback-", dir="/tmp/opencode") as directory:
        trees = {commit: Path(directory) / commit for commit in (BASELINE, CURRENT)}
        for commit, destination in trees.items():
            export_gateway(commit, destination)
        producer, consumer = (CURRENT, BASELINE) if rollback else (BASELINE, CURRENT)
        async with dedicated_pool() as pool:
            client = uuid4()
            scope = f"qualification:rollback:{client}"
            try:
                assert not await pool.fetchval("select exists(select 1 from public.workflow_jobs)")
                await pool.execute(
                    "insert into public.gateway_clients(id,name,spending_limit) "
                    "values($1,'Synthetic version switch',10)", client,
                )
                produced = await probe(trees[producer], client, "produce")
                consumed = await probe(trees[consumer], client, "consume")
                assert produced["pid"] != consumed["pid"]
                assert await pool.fetchval(
                    "select status from public.workflow_jobs where client_id=$1", client,
                ) == "completed"
                rows = await pool.fetch(
                    "select status,actual_cost from public.financial_reservations "
                    "where client_id=$1",
                    client,
                )
                assert len(rows) == 2
                assert all(row["status"] == "settled" and row["actual_cost"] == Decimal("0.25")
                           for row in rows)
                assert await pool.fetchval(
                    "select count(*) from public.settlement_outbox o join "
                    "public.financial_reservations f on f.id=o.reservation_id "
                    "where f.client_id=$1 and o.applied_at is not null", client,
                ) == 2
                assert await pool.fetchval(
                    "select reserved_cost from public.client_spend_windows where client_id=$1",
                    client,
                ) == Decimal("0.50")
                assert await pool.fetchval(
                    "select count(*) from public.request_attempts a join public.request_logs r "
                    "on r.id=a.request_id where r.client_id=$1", client,
                ) == 1
                assert not await pool.fetchval(
                    "select exists(select 1 from public.capacity_leases where scope=$1)", scope,
                )
            finally:
                await pool.execute("delete from public.capacity_leases where scope=$1", scope)
                await pool.execute("delete from public.capacity_scopes where scope=$1", scope)
                await pool.execute("delete from public.settlement_outbox where reservation_id in "
                                   "(select id from public.financial_reservations "
                                   "where client_id=$1)",
                                   client)
                await pool.execute("delete from public.financial_reservations where client_id=$1",
                                   client)
                await pool.execute("delete from public.workflow_jobs where client_id=$1", client)
                await pool.execute("delete from public.request_logs where client_id=$1", client)
                await pool.execute("delete from public.gateway_clients where id=$1", client)
