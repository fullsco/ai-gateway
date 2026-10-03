"""M0 local mocked capacity evidence, never provider-throughput qualification.

Run: .venv/bin/python scripts/benchmark_admission.py --output /tmp/opencode/baseline.json
Default: bounded synchronized bursts; --mode closed-loop --waves 4 for sustained workers.
All payload classes run healthy; impairment cases use the smallest selected body class.
ASGI send backpressure emulates slow readers; it does not model TCP/kernel buffers.
"""

import argparse
import asyncio
import contextvars
import hashlib
import importlib.metadata
import json
import logging
import math
import os
import platform
import resource
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CURRENT = contextvars.ContextVar("benchmark_record")
CONCURRENCY = (1, 8, 32, 64, 128)


def bootstrap():
    # Importing this script is inert. Guards precede all gateway.app imports.
    sys.path.insert(0, str(ROOT))
    from scripts.dev_check import install_offline_guards

    install_offline_guards()
    os.environ["GATEWAY_ENVIRONMENT"] = "test"


def distribution(values):
    ordered = sorted(values)

    def quantile(p):
        if not ordered:
            return None
        index = (len(ordered) - 1) * p
        lo, hi = math.floor(index), math.ceil(index)
        return round(ordered[lo] + (ordered[hi] - ordered[lo]) * (index - lo), 6)

    return {"count": len(ordered), "p50": quantile(.5), "p95": quantile(.95),
            "p99": quantile(.99), "max": ordered[-1] if ordered else None}


def rss_bytes():
    # Current resident pages, not ru_maxrss (which is lifetime high-water).
    if sys.platform == "linux":
        return int(Path("/proc/self/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value if sys.platform == "darwin" else value * 1024


def metadata():
    packages = ("fastapi", "starlette", "httpx", "pydantic", "pydantic-settings",
                "asyncpg", "uvicorn", "anyio", "cryptography")
    # Hash source and lock file, never settings files, paths, hostnames or environment values.
    digest = hashlib.sha256()
    for path in sorted((ROOT / "gateway").rglob("*.py")):
        digest.update(str(path.relative_to(ROOT)).encode())
        digest.update(path.read_bytes())
    lock = ROOT / "requirements.lock"
    runner = hashlib.sha256()
    for filename in ("benchmark_admission.py", "benchmark_fixtures.py", "dev_check.py"):
        runner.update((ROOT / "scripts" / filename).read_bytes())
    return {
        "python": platform.python_version(), "implementation": platform.python_implementation(),
        "os": platform.system(), "kernel": platform.release(), "architecture": platform.machine(),
        "cpu_count": os.cpu_count(),
        "cpu_affinity_count": (
            len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None
        ),
        "physical_memory_bytes": os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE"),
        "dependencies": {name: importlib.metadata.version(name) for name in packages},
        "gateway_source_sha256": digest.hexdigest(), "runner_sha256": runner.hexdigest(),
        "lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest(),
        "event_loop": type(asyncio.get_running_loop()).__name__,
    }


class Exchange:
    def __init__(self, path, body, key, scenario, options, admission):
        self.body, self.scenario, self.options = body, scenario, options
        self.admission = admission
        self.offset = 0
        self.disconnect = asyncio.Event()
        self.record = {"attempts": 0, "streams_opened": 0, "streams_closed": 0,
                       "status": 0, "received_bytes": 0, "response_bytes": 0,
                       "peak_in_flight": 0, "peak_body_bytes": 0, "complete": False}
        headers = [(b"content-type", b"application/json"),
                   (b"authorization", f"Bearer {key}".encode()), (b"x-api-key", key.encode())]
        # Delayed uploads have no Content-Length: exercise actual-byte admission.
        if scenario not in {"upload_delay", "upload_timeout"}:
            headers.append((b"content-length", str(len(body)).encode()))
        self.scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
                      "http_version": "1.1", "method": "POST", "path": path,
                      "raw_path": path.encode(), "query_string": b"", "root_path": "",
                      "scheme": "http", "server": ("127.0.0.1", 80),
                      "client": ("127.0.0.1", 12345), "headers": headers}

    def sample_admission(self):
        for metric, attr in (("peak_in_flight", "_in_flight"), ("peak_body_bytes", "_body_bytes")):
            self.record[metric] = max(self.record[metric], getattr(self.admission, attr))

    async def receive(self):
        self.sample_admission()
        if self.offset < len(self.body):
            self.record.setdefault("upload_start", time.perf_counter())
            delay = self.options.delay if self.scenario == "upload_delay" else 0
            if self.scenario == "upload_timeout":
                delay = self.options.upload_timeout * 2
            await asyncio.sleep(delay)
            end = min(self.offset + max(1, math.ceil(len(self.body) / 4)), len(self.body))
            chunk = self.body[self.offset:end]
            self.offset = end
            self.record["received_bytes"] += len(chunk)
            if end == len(self.body):
                self.record["upload_complete"] = time.perf_counter()
            return {"type": "http.request", "body": chunk, "more_body": end < len(self.body)}
        await self.disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(self, message):
        self.sample_admission()
        if message["type"] == "http.response.start":
            self.record["status"] = message["status"]
            self.record["headers"] = time.perf_counter()
            self.record["retry_after"] = b"retry-after" in dict(message.get("headers", []))
        elif message["type"] == "http.response.body":
            size = len(message.get("body", b""))
            if size:
                self.record.setdefault("first_byte", time.perf_counter())
                self.record["response_bytes"] += size
                if self.scenario == "disconnect" and self.record["status"] == 200:
                    self.record["disconnected"] = True
                    self.disconnect.set()
                    # Backpressure lets StreamingResponse's disconnect listener cancel relay.
                    await asyncio.sleep(self.options.delay)
                if self.scenario == "slow_reader":
                    await asyncio.sleep(self.options.delay)
            if not message.get("more_body", False):
                self.record["complete"] = True

    async def run(self, app):
        token = CURRENT.set(self.record)
        self.record["start"] = time.perf_counter()
        try:
            await asyncio.wait_for(app(self.scope, self.receive, self.send), 10)
        except Exception:
            # Fixed label; never serialize exception text/payload/credentials.
            self.record["exception"] = True
        finally:
            self.record["end"] = time.perf_counter()
            CURRENT.reset(token)
        return self.record


def summarize(records, elapsed, lag, rss, labels):
    metrics = {}
    for name, start, end in (
        ("total_ms", "start", "end"), ("upload_ms", "upload_start", "upload_complete"),
        ("ingress_to_dispatch_ms", "start", "dispatch"),
        ("post_upload_to_dispatch_ms", "upload_complete", "dispatch"),
        ("ttfb_ms", "start", "first_byte"), ("mock_headers_ms", "dispatch", "upstream_headers"),
        ("mock_ttft_ms", "dispatch", "upstream_first_byte"),
        ("response_lifetime_ms", "headers", "end"),
    ):
        metrics[name] = distribution([(r[end] - r[start]) * 1000 for r in records
                                      if start in r and end in r])
    successful = [r for r in records if r["status"] == 200 and r["complete"]
                  and not r.get("disconnected") and not r.get("exception")]
    rejected = [r for r in records if r["status"] in {408, 413, 429}]
    overload = [r for r in records if r["status"] == 429]
    metrics["completed_total_ms"] = distribution([(r["end"] - r["start"]) * 1000
                                                for r in successful])
    metrics["overload_response_ms"] = distribution([(r["headers"] - r["start"]) * 1000
                                                   for r in overload])
    metrics["event_loop_lag_ms"] = distribution(lag)
    return {**labels, "requests": len(records), "elapsed_seconds": elapsed,
            "attempted_rps": len(records) / elapsed, "completed_rps": len(successful) / elapsed,
            "completed": len(successful), "rejections": len(rejected),
            "status_counts": dict(Counter(str(r["status"]) for r in records)),
            "overload_retry_after_missing": sum(not r["retry_after"] for r in overload),
            "disconnects": sum(bool(r.get("disconnected")) for r in records),
            "exceptions": sum(bool(r.get("exception")) for r in records),
            "upstream_attempts": sum(r["attempts"] for r in records),
            "retries": sum(max(0, r["attempts"] - 1) for r in records),
            "streams_opened": sum(r["streams_opened"] for r in records),
            "streams_closed": sum(r["streams_closed"] for r in records),
            "received_bytes": sum(r["received_bytes"] for r in records),
            "response_bytes": sum(r["response_bytes"] for r in records),
            "peak_in_flight": max(r["peak_in_flight"] for r in records),
            "peak_body_bytes": max(r["peak_body_bytes"] for r in records),
            "rss_start_bytes": rss[0], "rss_peak_bytes": max(rss), "rss_end_bytes": rss[-1],
            "metrics": metrics}


async def run_case(options, protocol, scenario, size, concurrency):
    from gateway.admission import InferenceAdmissionMiddleware
    from gateway.app import create_app
    from gateway.config import Settings
    from gateway.metrics import metrics
    from scripts.benchmark_fixtures import PROTOCOLS, build_runtime, payload

    runtime, key = build_runtime(protocol, scenario, CURRENT, time.perf_counter, options.delay)
    app = create_app(Settings(
        _env_file=None, environment="test", database_url=None, supabase_url=None,
        health_probe_enabled=False, alert_monitor_enabled=False,
        credential_usage_poll_enabled=False,
        max_in_flight_requests=options.capacity, max_request_body_bytes=options.max_body,
        max_in_flight_per_client=options.capacity, max_in_flight_streams=options.capacity,
        max_in_flight_body_bytes=options.body_budget,
        request_body_timeout_seconds=options.upload_timeout,
        log_level="CRITICAL", failover_enabled=False, stream_keepalive_seconds=0,
    ), runtime)
    app.middleware_stack = app.build_middleware_stack()
    admission = app.middleware_stack
    while not isinstance(admission, InferenceAdmissionMiddleware):
        admission = admission.app
    body = payload(protocol, scenario, options.max_body + 1 if scenario == "oversize" else size)
    path = PROTOCOLS[protocol][2]
    records, lag, rss = [], [], [rss_bytes()]
    stop = asyncio.Event()

    async def sampler():
        while not stop.is_set():
            before = time.perf_counter()
            await asyncio.sleep(.005)
            lag.append(max(0, time.perf_counter() - before - .005) * 1000)
            rss.append(rss_bytes())

    async def request():
        result = await Exchange(path, body, key, scenario, options, admission).run(app)
        records.append(result)

    async def worker():
        for _ in range(options.waves):
            await request()

    # One excluded warm-up initializes framework/translator paths per case.
    monitor = None
    try:
        await request()
        records.clear()
        stages_before = metrics.snapshot()["stages"]
        monitor = asyncio.create_task(sampler())
        await asyncio.sleep(0)
        start = time.perf_counter()
        if options.mode == "burst":
            for _ in range(options.waves):
                await asyncio.gather(*(request() for _ in range(concurrency)))
        else:
            await asyncio.gather(*(worker() for _ in range(concurrency)))
        elapsed = time.perf_counter() - start
    finally:
        stop.set()
        if monitor:
            await monitor
        await runtime.http_client.aclose()
    result = summarize(records, elapsed, lag, rss, {
        "protocol": protocol, "scenario": scenario, "body_bytes": len(body),
        "concurrency": concurrency, "mode": options.mode,
    })
    result["admission_drained"] = admission._in_flight == admission._body_bytes == 0
    result["stage_totals"] = {
        stage: {key: value[key] - stages_before[stage][key] for key in ("count", "errors", "sum")}
        for stage, value in metrics.snapshot()["stages"].items()
    }
    expected = {200, 429}
    if scenario in {"upload_timeout", "oversize", "upstream_error"}:
        expected = {"upload_timeout": {408, 429}, "oversize": {413},
                    "upstream_error": {503, 429}}[scenario]
    result["qualified"] = (
        result["admission_drained"] and not result["exceptions"]
        and not result["overload_retry_after_missing"]
        and result["streams_opened"] == result["streams_closed"]
        and all(r["status"] in expected for r in records)
        and any(r["status"] != 429 for r in records)
        and (scenario != "disconnect" or result["disconnects"] > 0)
        and all(r["complete"] or r.get("disconnected") for r in records)
        and result["peak_in_flight"] <= options.capacity
        and result["peak_body_bytes"] <= options.body_budget
        and (scenario not in {"oversize", "upload_timeout"} or result["upstream_attempts"] == 0)
    )
    return result


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--concurrency", type=int, nargs="+", default=list(CONCURRENCY))
    parser.add_argument("--sizes", type=int, nargs="+", default=[8192, 262144, 1048576, 8388608])
    parser.add_argument("--protocols", nargs="+", default=["chat_native", "responses_native",
                                                        "messages_native", "messages_via_chat"])
    parser.add_argument("--scenarios", nargs="+", default=["healthy", "stream", "slow_stream",
                        "reasoning_pause", "slow_reader", "disconnect", "upload_delay",
                        "upload_timeout", "upstream_error", "oversize"])
    parser.add_argument("--waves", type=int, default=1)
    parser.add_argument("--mode", choices=["burst", "closed-loop"], default="burst")
    parser.add_argument("--capacity", type=int, default=32)
    parser.add_argument("--max-body", type=int, default=8388608)
    parser.add_argument("--body-budget", type=int, default=67108864)
    parser.add_argument("--delay", type=float, default=.005)
    parser.add_argument("--upload-timeout", type=float, default=.25)
    args = parser.parse_args(argv)
    if not all(c in CONCURRENCY for c in args.concurrency):
        parser.error("concurrency must be selected from 1, 8, 32, 64, 128")
    if not (1 <= args.waves <= 8 and 1 <= args.capacity <= 128
            and 256 <= args.max_body <= 8388608 and args.max_body <= args.body_budget <= 67108864
            and .001 <= args.delay <= .1 and .05 <= args.upload_timeout <= 2
            and all(256 <= size <= args.max_body for size in args.sizes)):
        parser.error("workload exceeds bounded limits; inspect --help and reduce values")
    if (len(args.concurrency) > 5 or len(args.sizes) > 4 or len(args.protocols) > 4
            or len(args.scenarios) > 10):
        parser.error("matrix dimensions exceed bounded limits")
    target = args.output.resolve()
    if not target.is_relative_to(Path("/tmp/opencode")) or not target.parent.is_dir():
        parser.error("output must have an existing parent within /tmp/opencode")
    if target.exists():
        parser.error("output already exists; choose a fresh evidence filename")
    args.output = target
    return args


async def benchmark(options):
    from scripts.benchmark_fixtures import PROTOCOLS, SCENARIOS

    if set(options.protocols) - PROTOCOLS.keys() or set(options.scenarios) - set(SCENARIOS):
        raise ValueError("Unknown protocol/scenario; only synthetic fixture labels are allowed")
    logging.disable(logging.CRITICAL)
    cases = []
    report = {"schema_version": 1, "started_at": datetime.now(UTC).isoformat(),
              "kind": "in_process_asgi_mock", "metadata": metadata(),
              "configuration": {k: v for k, v in vars(options).items() if k != "output"},
              "measurement_notes": [
                  "No database, provider network, application lifespan or background workers.",
                  "One warm-up per case; healthy uses all sizes, impairments smallest size.",
                  "Quantiles use linear interpolation; small samples are not tail-latency SLOs.",
                  "5ms lag/RSS sampler; can miss peaks; RSS includes generator and prior cases.",
                  "Total includes rejection/disconnect; completed_total excludes them.",
                  "Upload is observed receive duration; post-upload includes parse/auth/routing.",
                  "ASGI return defines response lifetime including cancellation/cleanup.",
                  "Synthetic pause/backpressure do not qualify long-duration or TCP behavior.",
              ], "unmeasured": ["database_pool_wait_under_load",
                "financial_reservations", "http_connection_pool_wait", "durable_settlement",
                "production_metrics_export", "sampled_traces", "multi_process_capacity"],
              "cases": cases}
    for protocol in options.protocols:
        for scenario in options.scenarios:
            sizes = options.sizes if scenario == "healthy" else [min(options.sizes)]
            for size in sizes:
                for concurrency in options.concurrency:
                    cases.append(await run_case(options, protocol, scenario, size, concurrency))
    report["qualified"] = all(case["qualified"] for case in cases)
    report["finished_at"] = datetime.now(UTC).isoformat()
    return report


def main(argv=None):
    options = parse_args(argv)
    bootstrap()
    report = asyncio.run(benchmark(options))
    with options.output.open("x") as output:
        json.dump(report, output, indent=2, allow_nan=False)
        output.write("\n")
    print(json.dumps({"qualified": report["qualified"], "cases": len(report["cases"]),
                      "evidence": str(options.output)}))
    return 0 if report["qualified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
