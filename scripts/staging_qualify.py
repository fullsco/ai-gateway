"""Authorized AWS-only deployed-role qualification; all endpoints and DB are isolated loopback."""

import argparse
import asyncio
import json
import os
import secrets
import socket
import subprocess
import sys
from collections import Counter
from pathlib import Path
from time import monotonic
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if socket.gethostname() != "ip-172-31-42-222":
    raise SystemExit("Restricted to the authorized AWS staging server")

from gateway.config import Settings  # noqa: E402

Settings.model_config["env_file"] = None


async def seed():
    import asyncpg

    from gateway.security.gateway_keys import GatewayKeyHasher

    path = ROOT / ".staging-config"
    if path.exists():
        return json.loads(path.read_text())
    db = await asyncpg.connect((ROOT / ".staging-dsn").read_text().strip())
    config = {"clients": {}, "admin_token": secrets.token_urlsafe(32)}
    try:
        hasher = GatewayKeyHasher(b"s" * 32)
        async with db.transaction():
            for lane in ("interactive", "workflow"):
                client, key_id = uuid4(), uuid4()
                issued = hasher.issue(key_id=str(key_id), client_id=str(client))
                await db.execute("insert into public.gateway_clients(id,name,spending_limit) "
                                 "values($1,$2,10000)", client, f"Staging synthetic {lane}")
                await db.execute("insert into public.gateway_client_keys"
                                 "(id,client_id,key_prefix,key_digest) "
                                 "values($1,$2,$3,$4)", key_id, client,
                                 issued.record.key_prefix, issued.record.digest)
                config["clients"][lane] = {"id": str(client), "key_id": str(key_id),
                    "key": issued.plaintext, "prefix": issued.record.key_prefix,
                    "digest": issued.record.digest}
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(config, stream)
        return config
    finally:
        await db.close()


class Services:
    def __init__(self, separate=False, direct_db=False, trace_queries=False):
        self.processes = {}
        self.logs = []
        self.separate = separate
        self.direct_db = direct_db
        self.trace_queries = trace_queries
        self.service_pids = {}

    def unit(self, name):
        return f"gateway-qualification-isolated-{name}.service"

    def start(self, name, role, port=None):
        log = (ROOT / "evidence" / f"{name}.log").open("ab")
        self.logs.append(log)
        args = [str(ROOT / ".venv/bin/python"), str(ROOT / "scripts/staging_service.py"), role]
        if port:
            args += ["--port", str(port)]
        if self.direct_db:
            args += ["--direct-db"]
        if self.trace_queries and role == "api":
            args += ["--trace-queries"]
        if self.separate:
            args = ["sudo", "-n", "systemd-run", "--quiet", "--collect", "--wait", "--pipe",
                    "--unit=" + self.unit(name), "--property=User=ubuntu",
                    "--property=WorkingDirectory=" + str(ROOT),
                    "--property=RuntimeMaxSec=7800", "--property=TimeoutStopSec=25",
                    "--slice=gateway-qualification-services.slice", *args]
        self.processes[name] = subprocess.Popen(args, cwd=ROOT, stdout=log, stderr=log)

    async def stop(self, name, kill=False):
        process = self.processes.pop(name, None)
        if process is None:
            return
        self.service_pids.pop(name, None)
        if self.separate:
            command = ["sudo", "-n", "systemctl"]
            command += ["kill", "--signal=KILL"] if kill else ["stop"]
            await asyncio.to_thread(subprocess.run, [*command, self.unit(name)], check=True)
        if process.poll() is None:
            process.kill() if kill else process.terminate()
        try:
            await asyncio.wait_for(asyncio.to_thread(process.wait), 25)
        except TimeoutError:
            process.kill()
            await asyncio.to_thread(process.wait)

    async def close(self):
        for name in list(reversed(self.processes)):
            await self.stop(name)
        for log in self.logs:
            log.close()

    def rss(self):
        total = 0
        for name, process in self.processes.items():
            try:
                pid = process.pid
                if self.separate:
                    if name not in self.service_pids:
                        value = subprocess.check_output([
                            "systemctl", "show", self.unit(name), "--property=MainPID", "--value",
                        ], text=True).strip()
                        if value and int(value):
                            self.service_pids[name] = int(value)
                    pid = self.service_pids.get(name, 0)
                fields = Path(f"/proc/{pid}/statm").read_text().split()
                total += int(fields[1]) * os.sysconf("SC_PAGE_SIZE")
            except (FileNotFoundError, ProcessLookupError):
                pass
        return total


async def ready(http, port):
    for _ in range(100):
        try:
            if (await http.get(f"http://127.0.0.1:{port}/ready")).status_code == 200:
                return
        except Exception:
            pass
        await asyncio.sleep(.1)
    raise RuntimeError(f"Staging API {port} did not become ready")


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))] if ordered else None


async def qualify(
    seconds, concurrency=4, faults=True, separate=False, profile=False, direct_db=False,
    trace_queries=False,
):
    import asyncpg
    import httpx

    config = await seed()
    services = Services(separate, direct_db, trace_queries)
    db = await asyncpg.connect((ROOT / ".staging-dsn").read_text().strip(), command_timeout=10)
    headers = {"Authorization": "Bearer " + config["clients"]["interactive"]["key"]}
    workflow_headers = {"Authorization": "Bearer " + config["clients"]["workflow"]["key"]}
    admin = {"Authorization": "Bearer " + config["admin_token"]}
    report = dict(host=socket.gethostname(), duration_target_seconds=seconds,
                  interactive_clients=concurrency, fault_drills=faults, phases={},
                  separate_service_slice=separate, direct_database=direct_db,
                  request_query_tracing=trace_queries)
    try:
        services.start("dbproxy", "proxy")
        services.start("provider", "provider")
        await asyncio.sleep(1)
        services.start("api-a", "api", 18330)
        services.start("api-b", "api", 18331)
        services.start("worker", "workflow")
        services.start("maintenance", "maintenance")
        async with httpx.AsyncClient(timeout=30, trust_env=False) as http:
            await ready(http, 18330)
            await ready(http, 18331)
            requests_before = await db.fetchval("select count(*) from public.request_logs")
            jobs_before = await db.fetchval("select count(*) from public.workflow_jobs")
            costs_before = await db.fetchval(
                "select coalesce(sum(actual_cost),0) from public.financial_reservations "
                "where status='settled'"
            )
            metric_before = []
            for port in (18330, 18331):
                metric_before.append((await http.get(
                    f"http://127.0.0.1:{port}/api/admin/v1/metrics", headers=admin,
                )).json())
            await db.execute("update public.workflow_jobs set status='cancelled',lease_owner=null,"
                             "lease_expires_at=null where status not in "
                             "('completed','failed','expired','cancelled')")
            body = {"model": "qualification-model", "max_tokens": 32,
                    "messages": [{"role": "user", "content": "synthetic"}]}
            # Both actual API processes, actual DB, actual HTTP provider listener.
            results, latencies, workflow_ids, rss = Counter(), [], [], []
            status_latencies, workflow_responses = {}, Counter()
            profiler = None
            if profile:
                from scripts.staging_profile import StagingProfile

                profiler = StagingProfile()
                await profiler.start(db)
            start = monotonic()

            async def interactive(index):
                number = index
                while monotonic() - start < seconds:
                    port = 18330 if number % 4 < 2 else 18331
                    payload = {**body, "stream": number % 2 == 0,
                               "metadata": {"delay": .03 if number % 3 else .2}}
                    if number % 5 == 0:
                        payload["messages"] = [{"role": "user", "content": "x" * 262144}]
                    began = monotonic()
                    try:
                        response = await http.post(f"http://127.0.0.1:{port}/v1/chat/completions",
                                                   headers=headers, json=payload)
                        results[str(response.status_code)] += 1
                        if response.status_code == 200:
                            assert not payload["stream"] or "[DONE]" in response.text
                        else:
                            assert response.status_code == 429, response.text
                        elapsed = (monotonic() - began) * 1000
                        latencies.append(elapsed)
                        status_latencies.setdefault(str(response.status_code), []).append(elapsed)
                        # Bound offered retry pressure; report this pacing with the workload.
                        if response.status_code == 429:
                            await asyncio.sleep(.05)
                    except httpx.HTTPError:
                        results["transport_error"] += 1
                    number += 1

            async def workflows():
                while monotonic() - start < seconds:
                    response = await http.post("http://127.0.0.1:18330/v1/workflows",
                        headers={**workflow_headers, "Idempotency-Key": str(uuid4())},
                        json={"protocol": "openai_chat_completions",
                              "request": {**body, "metadata": {"delay": .1}}})
                    assert response.status_code in {202, 409, 429}, response.text
                    workflow_responses[str(response.status_code)] += 1
                    if response.status_code == 202:
                        workflow_ids.append(response.json()["id"])
                    await asyncio.sleep(.5)

            async def sample():
                while monotonic() - start < seconds:
                    rss.append(services.rss())
                    assert (await http.get("http://127.0.0.1:18330/health")).status_code == 200
                    if profiler:
                        await profiler.sample(db)
                    await asyncio.sleep(1)

            await asyncio.gather(
                *(interactive(i) for i in range(concurrency)), workflows(), sample(),
            )
            load_elapsed = monotonic() - start
            report["memory_samples_bytes"] = rss
            if profiler:
                report["profile"] = await profiler.finish(db)
            pending = 1
            for _ in range(180):
                pending = await db.fetchval("select count(*) from public.workflow_jobs "
                                            "where status in ('queued','running')")
                if pending == 0:
                    break
                await asyncio.sleep(.5)
            assert pending == 0
            statuses = await db.fetch("select status,count(*) as count "
                                       "from public.workflow_jobs where id=any($1::uuid[]) "
                                       "group by status", workflow_ids)
            workflows_completed = bool(workflow_ids) and (
                sum(row["count"] for row in statuses) == len(workflow_ids)
                and all(row["status"] == "completed" for row in statuses)
            )
            if faults:
                assert workflows_completed, statuses
            assert results["200"] > 0 and results["transport_error"] == 0
            report["phases"]["mixed_soak"] = {"elapsed_seconds": monotonic() - start,
                "load_elapsed_seconds": load_elapsed, "overload_retry_delay_seconds": .05,
                "responses": dict(results), "p50_ms": percentile(latencies, .5),
                "p95_ms": percentile(latencies, .95), "p99_ms": percentile(latencies, .99),
                "rss_peak_bytes": max(rss), "rss_first_bytes": rss[0], "rss_last_bytes": rss[-1],
                "workflow_submissions": len(workflow_ids),
                "workflow_responses": dict(workflow_responses),
                "latency_by_status": {
                    status: {"count": len(values), "p50_ms": percentile(values, .5),
                             "p95_ms": percentile(values, .95), "p99_ms": percentile(values, .99)}
                    for status, values in status_latencies.items()
                },
                "workflow_statuses": [dict(r) for r in statuses]}
            report["phases"]["mixed_soak"]["stage_deltas"] = []
            for index, port in enumerate((18330, 18331)):
                snapshot = (await http.get(
                    f"http://127.0.0.1:{port}/api/admin/v1/metrics", headers=admin,
                )).json()
                report["phases"]["mixed_soak"]["stage_deltas"].append({
                    stage: {
                        **{key: value[key] - metric_before[index]["stages"][stage][key]
                           for key in ("count", "sum", "errors")},
                        "buckets": [after - before for after, before in zip(
                            value["buckets"], metric_before[index]["stages"][stage]["buckets"],
                            strict=True)],
                    }
                    for stage, value in snapshot["stages"].items()
                })
            report["phases"]["mixed_soak"]["bucket_bounds_seconds"] = snapshot["bucket_bounds"]
            report["query_profiles"] = [
                (await http.get(f"http://127.0.0.1:{port}/qualification/query-profile",
                                headers=admin)).json() for port in (18330, 18331)
            ]
            assert await db.fetchval(
                "select count(*) from public.financial_reservations f "
                "join public.request_logs r on r.id=f.telemetry_request_id "
                "where r.status='succeeded' and f.status<>'settled'"
            ) == 0, "Successful staging calls must be durably settled before fault injection"
            report["phases"]["mixed_soak"]["successful_reservations_settled"] = True
            provider_state = (await http.get("http://127.0.0.1:18340/health")).json()
            assert provider_state["active"] == 0, provider_state
            assert provider_state["peak"] <= 8, provider_state
            report["phases"]["mixed_soak"]["provider"] = provider_state
            if not faults:
                report["qualified"] = workflows_completed
                if not workflows_completed:
                    report["qualification_failure"] = "Accepted workflows did not all complete"
                return report
            # Graceful replacement while SSE is active.
            async with http.stream("POST", "http://127.0.0.1:18330/v1/chat/completions",
                                   headers=headers, json={**body, "stream": True,
                                   "metadata": {"delay": 2}}) as stream:
                assert stream.status_code == 200
                iterator = stream.aiter_bytes()
                first = await anext(iterator)
                stopping = asyncio.create_task(services.stop("api-a"))
                alternative = await http.post("http://127.0.0.1:18331/v1/chat/completions",
                                               headers=headers, json=body)
                assert alternative.status_code == 200
                remaining = first + b"".join([chunk async for chunk in iterator])
                assert b"[DONE]" in remaining
                await stopping
            services.start("api-a", "api", 18330)
            await ready(http, 18330)
            report["phases"]["rolling_restart"] = "passed"
            # Isolated DB proxy failure affects only this qualification stack.
            await services.stop("dbproxy", kill=True)
            failure = await http.post("http://127.0.0.1:18330/v1/chat/completions",
                                      headers=headers, json=body)
            assert failure.status_code == 503, failure.text
            assert (await http.get("http://127.0.0.1:18331/health")).status_code == 200
            services.start("dbproxy", "proxy")
            await asyncio.sleep(1)
            recovered = await http.post("http://127.0.0.1:18330/v1/chat/completions",
                                        headers=headers, json=body)
            assert recovered.status_code == 200, recovered.text
            report["phases"]["database_connection_loss"] = "passed"
            # Drop packets only to the dedicated proxy port; never alter SSH/other projects' DB.
            rule = ["sudo", "-n", "iptables", "-I", "OUTPUT", "-p", "tcp", "-d", "127.0.0.1",
                    "--dport", "18432", "-j", "DROP"]
            undo = rule.copy()
            undo[3] = "-D"
            subprocess.run(rule, check=True)
            try:
                began = monotonic()
                response = await http.post("http://127.0.0.1:18331/v1/chat/completions",
                                            headers=headers,
                                            json=body, timeout=25)
                assert response.status_code == 503
                assert monotonic() - began < 25
            finally:
                subprocess.run(undo, check=True)
            await asyncio.sleep(2)
            recovered = await http.post("http://127.0.0.1:18331/v1/chat/completions",
                                        headers=headers, json=body)
            assert recovered.status_code == 200, recovered.text
            report["phases"]["database_packet_partition"] = "passed"
            # Interrupt a live SSE's coordination renewals, not only a new request.
            before_partition = (await http.get("http://127.0.0.1:18340/health")).json()
            assert before_partition["active"] == 0, before_partition
            async with http.stream("POST", "http://127.0.0.1:18331/v1/chat/completions",
                headers=headers, json={**body, "stream": True, "metadata": {"delay": 45}},
                timeout=40) as response:
                assert response.status_code == 200
                iterator = response.aiter_bytes()
                await anext(iterator)
                subprocess.run(rule, check=True)
                interrupted = False
                began = monotonic()
                async def upstream_cancelled():
                    async with asyncio.timeout(29):
                        while True:
                            state = (await http.get("http://127.0.0.1:18340/health")).json()
                            if state["active"] == 0:
                                return monotonic() - began
                            await asyncio.sleep(.2)

                cancellation_probe = asyncio.create_task(upstream_cancelled())
                try:
                    async with asyncio.timeout(40):
                        try:
                            async for _ in iterator:
                                pass
                        except httpx.HTTPError:
                            interrupted = True
                    interrupted = True
                finally:
                    subprocess.run(undo, check=True)
                assert interrupted
                cancelled_at = await cancellation_probe
                report["phases"]["active_stream_partition"] = {
                    "cancelled_after_seconds": monotonic() - began,
                    "upstream_cancelled_seconds": cancelled_at,
                }
            await asyncio.sleep(3)
            # Kill a real worker after its job has entered execution; no blind replay.
            slow_job = await http.post("http://127.0.0.1:18330/v1/workflows",
                headers={**workflow_headers, "Idempotency-Key": str(uuid4())},
                json={"protocol": "openai_chat_completions",
                      "request": {**body, "metadata": {"delay": 45}}})
            assert slow_job.status_code == 202
            from uuid import UUID

            job_id = UUID(slow_job.json()["id"])
            for _ in range(50):
                dispatched = await db.fetchval(
                    "select exists(select 1 from public.financial_reservations "
                    "where workflow_id=$1 and status='dispatched')", job_id)
                if dispatched:
                    break
                await asyncio.sleep(.2)
            assert dispatched
            await services.stop("worker", kill=True)
            services.start("worker", "workflow")
            for _ in range(80):
                state = await db.fetchval(
                    "select status from public.workflow_jobs where id=$1", job_id,
                )
                if state == "waiting_for_approval":
                    break
                await asyncio.sleep(.5)
            assert state == "waiting_for_approval", state
            assert await db.fetchval("select count(*) from public.financial_reservations "
                                      "where workflow_id=$1", job_id) == 1
            report["phases"]["worker_crash_recovery"] = "paused_without_replay"
            await http.post("http://127.0.0.1:18330/api/admin/v1/drain", headers=admin)
            assert (await http.get("http://127.0.0.1:18330/ready")).status_code == 503
            report["phases"]["drain_readiness"] = "passed"
            await asyncio.sleep(6)
            counts = await db.fetch("select status,count(*) as count "
                                    "from public.financial_reservations group by status")
            report["accounting"] = [dict(row) for row in counts]
            report["run_deltas"] = {
                "request_logs": (await db.fetchval("select count(*) from public.request_logs")
                                 - requests_before),
                "workflow_jobs": (await db.fetchval("select count(*) from public.workflow_jobs")
                                  - jobs_before),
                "settled_cost": str(await db.fetchval("select coalesce(sum(actual_cost),0) "
                    "from public.financial_reservations where status='settled'") - costs_before),
            }
            report["qualified"] = True
    finally:
        await services.close()
        await db.close()
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, default=120, choices=range(30, 7201))
    parser.add_argument("--clients", type=int, default=4, choices=[1, 4, 8, 32, 64, 128])
    parser.add_argument("--skip-faults", action="store_true",
                        help="Run workload only; fault drills are separately verified")
    parser.add_argument("--separate-services", action="store_true",
                        help="Launch services in a separately resource-limited systemd slice")
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--direct-db", action="store_true")
    parser.add_argument("--trace-queries", action="store_true")
    args = parser.parse_args()
    if args.profile and not args.separate_services:
        parser.error("--profile requires --separate-services")
    if args.direct_db and not args.skip_faults:
        parser.error("--direct-db requires --skip-faults; partition drills require the proxy")
    (ROOT / "evidence").mkdir(exist_ok=True)
    try:
        report = asyncio.run(qualify(args.seconds, args.clients, not args.skip_faults,
            args.separate_services, args.profile, args.direct_db, args.trace_queries))
    except Exception as error:
        (ROOT / "evidence/failure.json").write_text(json.dumps({"error_type": type(error).__name__,
                                                              "message": str(error)[:1000]}))
        raise
    (ROOT / "evidence/qualification.json").write_text(json.dumps(report, indent=2, default=str))
    from scripts.qualification_report import analyze_stability, validate_functional_report

    report["analysis"] = analyze_stability(report)
    archive = ROOT / "evidence" / f"run-{args.clients}-{args.seconds}s-{uuid4().hex}.json"
    archive.write_text(json.dumps(report, indent=2, default=str))
    if args.skip_faults:
        output = ROOT / "evidence" / f"ladder-{args.clients}-{args.seconds}s.json"
        output.write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps(report, indent=2, default=str))
    validate_functional_report(report)
    if not report.get("qualified"):
        raise SystemExit("Functional qualification failed")
