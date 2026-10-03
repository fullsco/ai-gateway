"""AWS-only synthetic operations qualification; lead executes after review."""

import asyncio
import json
import os
import signal
import socket
import stat
import sys
from pathlib import Path
from time import monotonic
from urllib.parse import urlsplit
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
HOST, DATABASE = "ip-172-31-42-222", "gateway_qualification"
API = "http://127.0.0.1:18330"


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def private_text(name):
    fd = os.open(ROOT / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600
                and info.st_uid == os.geteuid(), f"{name} must be owned, regular, mode 0600")
        require(info.st_size <= 65536, f"{name} exceeds input limit")
        return stream.read()


def inputs():
    require(socket.gethostname() == HOST, "Restricted to authorized AWS staging host")
    # No dotenv, libpq, proxy, Python path, or inherited gateway configuration.
    for name in list(os.environ):
        if name not in {"PATH", "HOME", "LANG", "LC_ALL", "VIRTUAL_ENV"}:
            os.environ.pop(name)
    dsn = private_text(".staging-dsn").strip()
    parsed = urlsplit(dsn)
    # The existing proxy hardcodes IPv4:5432 and rewrites the literal :5432/.
    require(parsed.scheme == "postgresql" and parsed.hostname == "127.0.0.1"
            and parsed.port == 5432 and parsed.path == "/" + DATABASE
            and parsed.username == DATABASE and bool(parsed.password)
            and not parsed.query and not parsed.fragment and ":5432/" in dsn,
            "DSN must explicitly target loopback:5432/gateway_qualification without options")
    config = json.loads(private_text(".staging-config"))
    require(isinstance(config.get("admin_token"), str) and len(config["admin_token"]) >= 32,
            "Missing synthetic administrator token")
    require(set(config["clients"]) == {"interactive", "workflow"}, "Invalid synthetic lanes")
    for client in config["clients"].values():
        UUID(client["id"])
        UUID(client["key_id"])
        require(all(isinstance(client.get(key), str) and client[key]
                    for key in ("key", "prefix", "digest")), "Invalid synthetic client key")
    return dsn, config


async def wait_ready(http, port, status, seconds):
    import httpx

    attempts, began = [], monotonic()
    try:
        async with asyncio.timeout(seconds):
            while True:
                try:
                    response = await http.get(f"http://127.0.0.1:{port}/ready")
                    attempts.append(response.status_code)
                    if response.status_code == status:
                        require(response.json()["status"] ==
                                ("ready" if status == 200 else "not_ready"), "Invalid ready body")
                        return {"seconds": monotonic() - began, "statuses": attempts}
                except httpx.HTTPError:
                    attempts.append("transport_error")
                await asyncio.sleep(.05)
    except TimeoutError:
        raise RuntimeError(f"API {port}: readiness {status} not seen within {seconds}s") from None


def verify_operations(body, unavailable=False, draining=False):
    database = body["database"]
    require(database in {"available", "unavailable"}, "Database availability not explicit")
    require(not unavailable or database == "unavailable", "Outage falsely reported available")
    require(body["ready"] is True and body["draining"] is draining, "Wrong process state")
    envelope = body["operating_envelope"]
    statuses, missing = envelope["statuses"], envelope["missing_signal_reasons"]
    require(envelope["advisory_only"] is True and envelope["scale_out_authorized"] is False,
            "Envelope must remain advisory")
    require(envelope["scope"]["dependency_healthy"] == "database_only"
            and envelope["scope"]["deployment"] == "unknown", "Misleading envelope scope")
    require(envelope["readiness"] == {"process_ready": not draining, "database": database},
            "Envelope readiness disagrees with availability")
    require(envelope["recommendation"] == "investigate"
            and envelope["current_replicas"] == {"value": None, "source": "unknown"}
            and missing.get("current_replicas")
            and envelope["connection_budget_basis"] == "policy_estimate_not_measured_deployment",
            "Missing deployment evidence must block scaling")
    for name in ("config_age_seconds", "event_loop_lag_seconds"):
        require(statuses[name]["status"] == "unknown" and missing.get(name),
                f"Missing {name} must be explicit")
    for name, entry in statuses.items():
        if entry["status"] == "unknown":
            require(entry["value"] is None and missing.get(name), "Unknown signal lacks reason")
    dependency = statuses["dependency_healthy"]
    require(dependency["value"] is (database == "available")
            and dependency["status"] == ("healthy" if database == "available" else "warning"),
            "Envelope contradicts database availability")
    if database == "unavailable":
        require("database_unavailable" in body["warnings"], "Unavailable warning missing")
        for name in ("unknown_usage", "settlement_backlog", "queue_age_seconds"):
            require(statuses[name]["status"] == "unknown", "Partial query data presented as known")
    else:
        require(all(name in body for name in ("workflows", "accounting", "capacity", "pool")),
                "Available all-feature response lacks observations")
        require("database_unavailable" not in body["warnings"], "Contradictory database warning")
        for name in ("unknown_usage", "settlement_backlog"):
            value = body["accounting"][name]
            require(type(value) is int and value >= 0 and statuses[name]["value"] == value,
                    "Accounting observation disagrees with envelope")
            require((name in body["warnings"]) == (value > 0), "Accounting warning is untruthful")
        # Cumulative synthetic unknown holds are valid evidence, not a clean-DB failure.
        if body["accounting"]["unknown_usage"]:
            require(statuses["unknown_usage"]["status"] == "warning", "Unknown hold hidden")


async def plans(db, report):
    scopes = await db.fetch("select scope from public.capacity_scopes order by scope limit 100")
    queries = {
        "capacity_scoped_count": ("select scope,count(*) as used from public.capacity_leases "
                                  "where scope=any($1::text[]) group by scope",
                                  [row["scope"] for row in scopes] or ["requests"]),
        "queued_workflow_claim_selection": (
            "select * from public.workflow_jobs where status='queued' "
            "and deadline>clock_timestamp() and attempts<max_attempts "
            "and (select count(*) from public.workflow_jobs running "
            "where running.client_id=workflow_jobs.client_id and running.status='running')<$1 "
            "order by (select count(*) from public.workflow_jobs running "
            "where running.client_id=workflow_jobs.client_id and running.status='running'), "
            "created_at limit 1", 1),
        "accounting_pending_selection": (
            "select reservation_id from public.settlement_outbox where applied_at is null "
            "and next_attempt_at<=clock_timestamp() order by next_attempt_at limit $1", 100),
        "accounting_operations_pending": (
            "select count(*) filter(where status='unknown') as unknown_usage,"
            "count(*) filter(where status='settlement_pending') as settlement_backlog,"
            "(select coalesce(greatest(0,extract(epoch from now()-min(created_at))),0) "
            "from public.settlement_outbox where applied_at is null) as oldest_settlement_seconds "
            "from public.financial_reservations where status<>'settled'",),
    }
    report["plans"] = {}
    async with db.transaction(readonly=True):
        for name, (sql, *parameters) in queries.items():
            raw = await db.fetchval("EXPLAIN (FORMAT JSON) " + sql, *parameters)
            report["plans"][name] = json.loads(raw) if isinstance(raw, str) else raw
    report["plan_scope"] = "Planner estimates only; no ANALYZE, row locks, or runtime cost claim"


async def run(report):
    dsn, config = inputs()
    sys.path.insert(0, str(ROOT))
    from gateway.config import Settings, get_settings

    Settings.model_config["env_file"] = None
    get_settings.cache_clear()
    import asyncpg
    import httpx

    from scripts.staging_qualify import Services

    services, db, monitor = Services(separate=False), None, None
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, asyncio.current_task().cancel)
    try:
        for port in (18330, 18331, 18340, 18432):
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", port))  # Refuse an already-running qualification stack.
        db = await asyncpg.connect(dsn=dsn, timeout=5, command_timeout=10,
                                  server_settings={"application_name": "staging-operations",
                                                   "default_transaction_read_only": "on"})
        require(await db.fetchval("select current_database()") == DATABASE, "Wrong database")
        require(await db.fetchval("select count(*) from public.gateway_clients where name not in "
                                  "('Synthetic Qualification','Staging synthetic interactive',"
                                  "'Staging synthetic workflow')") == 0, "Non-synthetic clients")
        await plans(db, report)
        report["connections"] = {"cap": 25, "role_pool_maximum": 20, "observer": 1,
                                 "scope": "all pg_stat_activity rows for target database",
                                 "sampling_seconds": .1, "samples": [], "peak": 0}

        async def sample():
            while True:
                count = await db.fetchval("select count(*) from pg_stat_activity where datname=$1",
                                          DATABASE)
                observed = report["connections"]
                observed["samples"].append(count)
                observed["peak"] = max(observed["peak"], count)
                require(count <= 25, "Global target database connections exceeded 25")
                await asyncio.sleep(.1)

        monitor = asyncio.create_task(sample())
        services.start("dbproxy", "proxy")
        services.start("provider", "provider")
        await asyncio.sleep(1)
        roles = (("api-a", "api", 18330), ("api-b", "api", 18331),
                 ("worker", "workflow", None), ("maintenance", "maintenance", None))
        for name, role, port in roles:
            services.start(name, role, port)
        async with httpx.AsyncClient(timeout=3, trust_env=False) as http:
            for port in (18330, 18331):
                report[f"ready_{port}"] = await wait_ready(http, port, 200, 30)
            admin = {"Authorization": "Bearer " + config["admin_token"]}

            async def operations(phase, **expected):
                response = await http.get(API + "/api/admin/v1/operations/health", headers=admin)
                require(response.status_code == 200, "Operations endpoint failed")
                report[phase] = response.json()
                verify_operations(report[phase], **expected)

            await operations("healthy_operations")
            # Start timing before kill/wait; cached 200s are permitted only inside this budget.
            proxy = services.processes["dbproxy"]
            began = monotonic()
            async with asyncio.timeout(2):
                proxy.kill()  # Keep its handle registered until wait completes, even on timeout.
                await asyncio.to_thread(proxy.wait)
                report["proxy_loss"] = await wait_ready(http, 18330, 503, 2)
            report["proxy_loss"]["kill_to_503_seconds"] = monotonic() - began
            require(report["proxy_loss"]["kill_to_503_seconds"] <= 2, "Readiness outage too slow")
            await operations("outage_operations", unavailable=True)
            services.processes.pop("dbproxy")
            services.start("dbproxy", "proxy")
            report["recovery"] = await wait_ready(http, 18330, 200, 30)
            await operations("recovered_operations")
            response = await http.post(API + "/api/admin/v1/drain", headers=admin, timeout=25)
            require(response.status_code in {200, 202} and response.json()["draining"] is True,
                    "Drain request failed")
            report["drain"] = await wait_ready(http, 18330, 503, 2)
            await operations("draining_operations", draining=True)
            require(all(p.poll() is None for p in services.processes.values()), "Role exited early")
            if monitor.done():
                await monitor
            require(report["connections"]["samples"], "Connection observer did not sample")
            report["qualified"] = True
    finally:
        if monitor:
            monitor.cancel()
            results = await asyncio.gather(monitor, return_exceptions=True)
            if any(isinstance(r, Exception) for r in results):
                report["qualified"] = False
                report["observer_failed"] = True
        # Services.close stops at its first exception; retain handles and reap every child instead.
        cleanup_errors = []
        for process in reversed(list(services.processes.values())):
            try:
                if process.poll() is None:
                    process.terminate()
                await asyncio.wait_for(asyncio.to_thread(process.wait), 25)
            except (Exception, asyncio.CancelledError):
                report["qualified"] = False
                try:
                    if process.poll() is None:
                        process.kill()
                    await asyncio.to_thread(process.wait)
                except Exception as error:
                    cleanup_errors.append(type(error).__name__)
        for log in services.logs:
            try:
                log.close()
            except Exception as error:
                cleanup_errors.append(type(error).__name__)
        if db:
            await db.close(timeout=5)
        if cleanup_errors:
            report.update(qualified=False, cleanup_errors=cleanup_errors)


if __name__ == "__main__":
    require(socket.gethostname() == HOST, "Restricted to authorized AWS staging host")
    report = {"qualified": False, "host": HOST, "database": DATABASE,
              "data_scope": "cumulative synthetic staging only", "separate_services": False}
    (ROOT / "evidence").mkdir(exist_ok=True)
    try:
        asyncio.run(run(report))
    except BaseException as error:
        report.update(qualified=False, error_type=type(error).__name__)
        # Driver exceptions can include DSNs, SQL arguments, and credentials; never serialize them.
    finally:
        (ROOT / "evidence/operations-final.json").write_text(json.dumps(report, indent=2))
    raise SystemExit(0 if report["qualified"] else 1)
