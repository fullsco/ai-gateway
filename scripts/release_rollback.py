"""Finite installed-wheel rollback/recovery through an isolated nginx ingress on AWS staging."""

import argparse
import asyncio
import json
import secrets
import signal
import sys
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from types import SimpleNamespace
from uuid import UUID, uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.release_drill_infra import (  # noqa: E402
    Processes,
    drop_database,
    fresh_database,
    ports_available,
    ready,
    verify_artifacts,
)
from scripts.release_support import PORTS  # noqa: E402
from scripts.staging_boundary import disable_dotenv, require_staging  # noqa: E402


def request(marker, *, tool=False, stream=False):
    body = {"model": "qualification-model", "messages": [{"role": "user", "content": marker}],
            "max_tokens": 64, "stream": stream}
    if tool:
        body["tools"] = [{"type": "function", "function": {"name": "double", "parameters": {
            "type": "object", "properties": {"value": {"type": "integer"}},
            "required": ["value"]}}}]
    return body


def workflow(marker, *, tool=False):
    return {"protocol": "openai_chat_completions", "request": request(marker, tool=tool),
            "deadline_seconds": 240, "max_steps": 2, "max_tool_calls": 1,
            "max_total_tokens": 1024, "max_cost": "2"}


async def fixture(db, dsn):
    from gateway.security.gateway_keys import GatewayKeyHasher
    from scripts.seed_dbtest import CREDENTIAL, MAPPING, PROVIDER

    client_id, key_id = uuid4(), uuid4()
    pepper = secrets.token_bytes(32)
    key = GatewayKeyHasher(pepper).issue(key_id=str(key_id), client_id=str(client_id))
    await db.execute("insert into public.gateway_clients(id,name,spending_limit) "
                     "values($1,'Installed release drill',10)", client_id)
    await db.execute("insert into public.gateway_client_keys(id,client_id,key_prefix,key_digest) "
                     "values($1,$2,$3,$4)", key_id, client_id,
                     key.record.key_prefix, key.record.digest)
    return {"dsn": dsn, "client": str(client_id), "key_id": str(key_id),
            "prefix": key.record.key_prefix, "digest": key.record.digest,
            "key": key.plaintext, "pepper": pepper.hex(), "provider": str(PROVIDER),
            "credential": str(CREDENTIAL), "mapping": str(MAPPING)}


async def pending_fixtures(db, config, approval_job):
    from gateway.accounting import FinancialLedger
    from gateway.workflow_context import active_workflow
    from gateway.workflow_store import WorkflowStore

    ledger = FinancialLedger(db)
    route = SimpleNamespace(canonical_model_id="qualification-model",
        credential=SimpleNamespace(credential_id=UUID(config["credential"])),
        provider_model=SimpleNamespace(provider_id=UUID(config["provider"]), route_id=None))
    pending = await ledger.reserve(request_id=str(uuid4()), attempt_number=1,
        client_id=UUID(config["client"]), route=route, currency="USD", estimated_cost=Decimal("1"))
    await ledger.dispatch(pending)

    async def unavailable(_):
        raise RuntimeError("Injected process loss after committed settlement intent")

    ledger.apply = unavailable
    await ledger.settle(pending, actual=Decimal("0.25"))
    store, owner = WorkflowStore(db), uuid4()
    job = await store.claim(owner)
    assert str(job["id"]) == approval_job
    token = active_workflow.set((job["id"], owner, job["version"]))
    try:
        unknown = await ledger.reserve(request_id=str(uuid4()), attempt_number=1,
            client_id=UUID(config["client"]), route=route, currency="USD",
            estimated_cost=Decimal("0.50"), estimated_tokens=16)
        await ledger.dispatch(unknown)
        await ledger.settle(unknown, actual=None)
        await store.transition(job["id"], owner, job["version"], "waiting_for_approval",
                               {"step": 1, "reason": "injected_unknown_usage"})
    finally:
        active_workflow.reset(token)
    return pending, unknown


async def exercise(http, db, config, processes, report):
    from gateway.accounting import FinancialLedger

    ingress = f"http://127.0.0.1:{PORTS['ingress']}"
    provider = f"http://127.0.0.1:{PORTS['provider']}"
    headers = {"Authorization": "Bearer " + config["key"], "Connection": "close"}

    async def identity(label):
        async with asyncio.timeout(15):
            while True:
                response = await http.get(ingress + "/qualification/provenance", headers=headers)
                if response.status_code == 200 and response.json()["revision"] == (
                    report["artifacts"][label]["revision"]
                ):
                    return response.json()
                await asyncio.sleep(.1)

    async def infer(marker, label):
        response = await http.post(ingress + "/v1/chat/completions", headers=headers,
                                   json=request(marker))
        assert response.status_code == 200, f"Inference returned {response.status_code}"
        assert response.headers["x-qualification-release"] == label
        assert response.json()["choices"][0]["message"]["content"] == "release-ok"

    async def submit(marker, *, tool=False, key=None):
        response = await http.post(ingress + "/v1/workflows",
            headers={**headers, "Idempotency-Key": key or str(uuid4())},
            json=workflow(marker, tool=tool))
        assert response.status_code == 202, f"Workflow submission returned {response.status_code}"
        return response.json()["id"]

    async def job_state(job_id, expected):
        async with asyncio.timeout(40):
            while True:
                response = await http.get(ingress + f"/v1/workflows/{job_id}", headers=headers)
                assert response.status_code == 200
                state = response.json()
                if state["status"] == expected:
                    return state
                assert state["status"] in {"queued", "running"}, state["status"]
                await asyncio.sleep(.1)

    current = await identity("current")
    baseline = (await http.get(f"http://127.0.0.1:{PORTS['baseline']}/qualification/provenance")).json()
    assert current["dependency_sha256"] == baseline["dependency_sha256"]
    report["installed_identity"] = {"current": current, "baseline": baseline}
    await infer("release:warmup", "current")
    replay_key = str(uuid4())
    tool_job = await submit("release:tool", tool=True, key=replay_key)
    assert await submit("release:tool", tool=True, key=replay_key) == tool_job
    processes.start("workflow")
    waiting = await job_state(tool_job, "waiting_for_tool")
    assert waiting["checkpoint"]["invocation_id"] == "release-call"
    await processes.stop("workflow")
    approval_job = await submit("release:uncertain")
    pending, unknown = await pending_fixtures(db, config, approval_job)
    queued_job = await submit("release:queued")
    assert (await job_state(queued_job, "queued"))["status"] == "queued"
    assert await db.fetchval("select status from financial_reservations where id=$1", pending) == (
        "settlement_pending")
    report["phases"]["accepted_before_rollback"] = {
        "queued": 1, "waiting_for_tool": 1, "waiting_for_approval": 1,
        "pending_settlement": 1, "duplicate_submission_same_job": True,
        "uncertain_and_settlement_fixture": "injected via real ledger/store, not provider failure"}

    began = monotonic()
    async with http.stream("POST", ingress + "/v1/chat/completions", headers=headers,
                           json=request("release:stream", stream=True)) as stream:
        assert stream.status_code == 200
        assert stream.headers["x-qualification-release"] == "current"
        parts = stream.aiter_bytes()
        first = await asyncio.wait_for(anext(parts), 10)
        assert first
        rollback_handoff = await processes.switch(True)
        await identity("baseline")
        assert processes.processes["current"].poll() is None
        assert processes.processes["baseline"].poll() is None
        blocked = await http.post(ingress + "/v1/workflows", headers={
            **headers, "Idempotency-Key": str(uuid4())}, json=workflow("release:queued"))
        assert blocked.status_code == 503
        direct_jobs = await http.post(f"http://127.0.0.1:{PORTS['jobs']}/v1/workflows",
                                     headers=headers, json=workflow("release:queued"))
        assert direct_jobs.status_code == 503
        preserved = await job_state(tool_job, "waiting_for_tool")
        assert preserved["checkpoint"] == waiting["checkpoint"]
        assert (await job_state(approval_job, "waiting_for_approval"))["status"] == (
            "waiting_for_approval")
        await infer("release:baseline", "baseline")
        stopping = asyncio.create_task(processes.stop("current"))
        await asyncio.sleep(.2)
        assert not stopping.done(), "API stopped before its active stream drained"
        await http.post(provider + "/release-stream")
        content = first + b"".join([part async for part in parts])
        assert content.count(b"data: [DONE]") == 1 and b'release-ok' in content
        await stopping
    report["phases"]["ingress_rollback"] = {
        "old_and_new_overlap": True, "new_inference_on_baseline": True,
        "new_workflows_rejected": True, "accepted_job_endpoints_retained": True,
        "active_stream_completed": True, "current_interactive_role_stopped": True,
        "elapsed_seconds": monotonic() - began, "nginx_handoff": rollback_handoff}

    processes.start("workflow")
    processes.start("maintenance")
    await job_state(queued_job, "completed")
    tool_result = await http.post(ingress + f"/v1/workflows/{tool_job}/tool-results",
        headers=headers, json={"invocation_id": "release-call", "result": {"answer": 14}})
    assert tool_result.status_code == 200
    completed = await job_state(tool_job, "completed")
    assert completed["result"]["choices"][0]["message"]["content"] == "release-ok"
    async with asyncio.timeout(20):
        while await db.fetchval(
            "select status from financial_reservations where id=$1", pending,
        ) != "settled":
            await asyncio.sleep(.1)
    spent = await db.fetchval("select reserved_cost from client_spend_windows where client_id=$1",
                             UUID(config["client"]))
    ledger = FinancialLedger(db)
    await ledger.apply(pending)
    await ledger.apply(pending)
    assert await db.fetchval("select reserved_cost from client_spend_windows where client_id=$1",
                            UUID(config["client"])) == spent
    assert await db.fetchval("select status from financial_reservations where id=$1", unknown) == (
        "unknown")
    await job_state(approval_job, "waiting_for_approval")
    for job_id, dispatches in ((tool_job, 2), (queued_job, 1)):
        rows = await db.fetch("select status,actual_cost from financial_reservations "
                              "where workflow_id=$1", UUID(job_id))
        assert len(rows) == dispatches
        assert all(row["status"] == "settled" and row["actual_cost"] == Decimal("0.01")
                   for row in rows)
    report["phases"]["compatible_drainers"] = {
        "completed_preexisting_workflows": 2, "tool_workflow_inference_dispatches": 2,
        "queued_workflow_inference_dispatches": 1, "settlement_replay_idempotent": True,
        "unknown_usage_retained": True, "approval_still_required": True}

    processes.start("current")
    await ready(http, PORTS["current"], processes.processes["current"])
    recovery_handoff = await processes.switch(False)
    await identity("current")
    await infer("release:recovered", "current")
    recovery_job = await submit("release:recovered")
    await job_state(recovery_job, "completed")
    assert await submit("release:tool", tool=True, key=replay_key) == tool_job
    report["phases"]["forward_recovery"] = {
        "current_inference_restored": True, "new_workflow_completed": True,
        "pre_rollback_idempotency_retained": True, "nginx_handoff": recovery_handoff}
    state = (await http.get(provider + "/health")).json()
    assert state["active"] == 0
    assert state["requests"] == {"release:warmup:initial": 1, "release:tool:initial": 1,
        "release:tool:final": 1, "release:stream:initial": 1, "release:baseline:initial": 1,
        "release:queued:initial": 1, "release:recovered:initial": 2}, state
    assert await db.fetchval("select reserved_cost from client_spend_windows where client_id=$1",
                            UUID(config["client"])) == Decimal("0.83")
    assert await db.fetchval("select count(*) from capacity_leases where scope like $1",
                            f"%{config['client']}%") == 0
    report["provider"] = state
    report["accounting"] = {"fixture_currency": "USD", "reserved_total": "0.83",
                            "known_charges": "0.33", "unknown_hold": "0.50"}


async def drill(artifacts, output):
    import asyncpg
    import httpx

    root = require_staging()
    manifest = verify_artifacts(artifacts)
    ports_available()
    report = {"qualified": False, "scope": "installed pinned releases on isolated staging",
              "baseline_scope": manifest["baseline_scope"], "artifacts": {
                  label: {k: v for k, v in entry.items() if k != "gateway_files"}
                  for label, entry in manifest["versions"].items()}, "phases": {}}
    name, db, processes = None, None, None
    try:
        name, dsn, migrations = await fresh_database()
        report["schema"] = {"migration_count": len(migrations), "last": migrations[-1],
                            "downgrade_ddl": False, "database": name}
        db = await asyncpg.create_pool(dsn, min_size=1, max_size=2, timeout=5,
                                      command_timeout=10, statement_cache_size=0)
        before = [dict(row) for row in await db.fetch(
            "select name,checksum from qualification_migrations order by name")]
        config = await fixture(db, dsn)
        with TemporaryDirectory(prefix="release-drill-", dir=root / "evidence") as directory:
            directory = Path(directory)
            with (directory / "config.json").open("x") as stream:
                (directory / "config.json").chmod(0o600)
                json.dump(config, stream)
            processes = Processes(directory, artifacts)
            try:
                async with httpx.AsyncClient(trust_env=False, timeout=35,
                    limits=httpx.Limits(max_connections=10, max_keepalive_connections=0)) as http:
                    for role in ("provider", "current", "jobs", "baseline"):
                        processes.start(role)
                        await ready(http, PORTS[role], processes.processes[role])
                    processes.ingress()
                    await ready(http, PORTS["ingress"], processes.processes["ingress"])
                    await exercise(http, db, config, processes, report)
            finally:
                await processes.close()
                report["exit_codes"] = processes.exit_codes
                report["lifecycles"] = [json.loads(path.read_text())
                    for path in directory.glob("*.json") if path.name != "config.json"]
            for name_and_generation, code in processes.exit_codes.items():
                role = name_and_generation.rsplit("-", 1)[0]
                expected = -signal.SIGTERM if role in {
                    "baseline", "current", "jobs", "provider",
                } else 0
                assert code == expected, (name_and_generation, code)
            assert len(report["lifecycles"]) == 7
            assert all(row["lifespan_closed"] and row["pool_closed"]
                       for row in report["lifecycles"])
        after = [dict(row) for row in await db.fetch(
            "select name,checksum from qualification_migrations order by name")]
        assert before == after
        report["schema"]["journal_unchanged"] = True
        report["qualified"] = True
    except Exception as error:
        report["failure"] = {"type": type(error).__name__, "phase": list(report["phases"])[-1:]
                             or ["startup"]}
        raise
    finally:
        if db is not None:
            await asyncio.wait_for(db.close(), 15)
        if name is not None:
            drop_database(name)
            report["database_cleanup"] = "disposable database removed and absence verified"
        report["ports_released"] = all(_port_closed(port) for port in PORTS.values())
        if not report["ports_released"]:
            report["qualified"] = False
        output.write_text(json.dumps(report, indent=2))
        assert report["ports_released"], "Release drill left a listener running"
    return report


def _port_closed(port):
    import socket

    with socket.socket() as connection:
        connection.settimeout(.1)
        return connection.connect_ex(("127.0.0.1", port)) != 0


if __name__ == "__main__":
    root = require_staging()
    disable_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.artifacts.resolve().is_relative_to(root):
        parser.error("Release artifacts must be inside the staging workspace")
    if not args.output.resolve().is_relative_to(root / "evidence"):
        parser.error("Output must be inside staging evidence")
    result = asyncio.run(asyncio.wait_for(drill(args.artifacts.resolve(), args.output), 300))
    print(json.dumps({"qualified": result["qualified"], "phases": result["phases"],
                      "database_cleanup": result["database_cleanup"]}))
