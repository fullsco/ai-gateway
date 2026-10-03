"""Opt-in staging request/driver correlation. No SQL, arguments, IDs or bodies retained."""

import asyncio
import functools
import json
import math
import random
from collections import deque
from contextlib import asynccontextmanager
from contextvars import ContextVar
from time import perf_counter

import asyncpg

from gateway.metrics import active_measurement, measurement_observer

SPAN_LIMIT = 128
QUERY_LIMIT = 256
RECENT_LIMIT = 128
SLOW_LIMIT = 64
SAMPLE_RATE = .1
SLOW_SECONDS = .1
OPERATIONS = frozenset({
    "SELECT", "INSERT", "UPDATE", "DELETE", "BEGIN", "START", "COMMIT", "ROLLBACK",
    "SET", "RESET", "WITH",
})
current_capture: ContextVar["RequestCapture | None"] = ContextVar(
    "qualification_capture", default=None,
)
current_query: ContextVar[dict | None] = ContextVar("qualification_query", default=None)


def query_operation(query):
    # Only the leading keyword is classified; SQL literals and bind values are never stored.
    prefix = query.lstrip()[:32].split(None, 1)
    operation = prefix[0].rstrip(";").upper() if prefix else "EMPTY"
    return operation if operation in OPERATIONS or operation == "EMPTY" else "OTHER"


def _stage_scope():
    scope = active_measurement.get()
    if scope is None:
        return None, "unscoped"
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    if scope.task is not task:
        # Task contexts are inherited by heartbeats and shielded pool release tasks.
        return None, "inherited_task"
    return scope, "stage_task"


class RequestCapture:
    def __init__(self, sequence):
        self.sequence = sequence  # Local ordinal, never a caller's request identifier.
        self.started = perf_counter()
        self.closed = False
        self.status = 500
        self.body_bytes = 0
        self.streaming = False
        self.predispatch = None
        self.spans = []
        self.queries = []
        self.dropped_spans = 0
        self.dropped_queries = 0

    def observe(self, stage, elapsed, error):
        if self.closed:
            return
        if stage == "gateway_pre_dispatch" and self.predispatch is None:
            self.predispatch = elapsed
        if len(self.spans) >= SPAN_LIMIT:
            self.dropped_spans += 1
            return
        finished = perf_counter()
        scope, origin = _stage_scope()
        started = (scope.started if scope is not None and scope.stage == stage
                   else finished - elapsed)
        self.spans.append({
            "stage": stage, "seconds": elapsed, "error": error, "origin": origin,
            "start_offset_seconds": started - self.started,
            "end_offset_seconds": finished - self.started,
        })

    def begin_query(self, query):
        if self.closed:
            return None
        if len(self.queries) >= QUERY_LIMIT:
            self.dropped_queries += 1
            return None
        scope, origin = _stage_scope()
        entry = {
            "operation": query_operation(query), "origin": origin,
            "stage": scope.stage if scope else None,
            "stage_start_offset_seconds": scope.started - self.started if scope else None,
            "start_offset_seconds": perf_counter() - self.started,
            "end_offset_seconds": None, "wall_seconds": None,
            "driver_seconds": None, "driver_callback_count": 0, "error": False,
        }
        self.queries.append(entry)
        return entry

    def snapshot(self):
        return {
            "sequence": self.sequence, "status": self.status, "body_bytes": self.body_bytes,
            "streaming": self.streaming, "predispatch_seconds": self.predispatch,
            "dropped_spans": self.dropped_spans, "dropped_queries": self.dropped_queries,
            "spans": [dict(span) for span in self.spans],
            "queries": [dict(query) for query in self.queries],
            "missing_driver_timings": sum(q["driver_seconds"] is None for q in self.queries),
        }


class RequestTraceCollector:
    def __init__(self, sample_rate=SAMPLE_RATE):
        if not math.isfinite(sample_rate) or not 0 <= sample_rate <= 1:
            raise ValueError("Sample rate must be finite and between zero and one")
        self.sample_rate = sample_rate
        self.recent = deque(maxlen=RECENT_LIMIT)
        self.slow = deque(maxlen=SLOW_LIMIT)
        self.sampled = 0
        self.completed = 0
        self.slow_completed = 0

    def begin(self):
        if random.random() >= self.sample_rate:
            return None
        self.sampled += 1
        return RequestCapture(self.sampled)

    def finish(self, capture):
        capture.closed = True
        self.completed += 1
        self.recent.append(capture)
        if capture.predispatch is not None and capture.predispatch > SLOW_SECONDS:
            self.slow_completed += 1
            self.slow.append(capture)

    def snapshot(self):
        # Known callback entries may finish after ASGI completion; snapshots copy their state.
        return {
            "scope": "process", "sample_rate": self.sample_rate, "sampled": self.sampled,
            "completed": self.completed, "slow_completed": self.slow_completed,
            "slow_threshold_seconds": SLOW_SECONDS,
            "limits": {"recent": RECENT_LIMIT, "slow": SLOW_LIMIT,
                       "spans_per_request": SPAN_LIMIT, "queries_per_request": QUERY_LIMIT},
            "recent": [capture.snapshot() for capture in self.recent],
            "slow": [capture.snapshot() for capture in self.slow],
            "limitations": [
                "SQL operation is its leading keyword, not a statement fingerprint.",
                "Driver and public-call wall times include transport and scheduling; not SQL CPU.",
                "Query durations are nested within stages; do not add them to enclosing stages.",
                "Inherited-task queries are kept separate from foreground stage attribution.",
                "Recent and slow rings overlap; deduplicate by sequence within this process only.",
                "Bounded sampled requests and a slow-biased ring cannot establish population p95.",
            ],
        }


class RequestTraceMiddleware:
    def __init__(self, app, collector):
        self.app, self.collector = app, collector

    async def __call__(self, scope, receive, send):
        if (scope["type"] != "http" or scope.get("method") != "POST" or scope.get("path") not in {
            "/v1/chat/completions", "/v1/responses", "/v1/messages",
        }):
            await self.app(scope, receive, send)
            return
        capture = self.collector.begin()
        if capture is None:
            await self.app(scope, receive, send)
            return
        token = current_capture.set(capture)
        observer = measurement_observer.set(capture.observe)

        async def read():
            message = await receive()
            if message["type"] == "http.request":
                capture.body_bytes += len(message.get("body", b""))
            return message

        async def write(message):
            if message["type"] == "http.response.start":
                capture.status = message["status"]
                capture.streaming = any(
                    key.lower() == b"content-type" and value.startswith(b"text/event-stream")
                    for key, value in message.get("headers", ())
                )
            await send(message)

        try:
            await self.app(scope, read, write)
        finally:
            measurement_observer.reset(observer)
            current_capture.reset(token)
            self.collector.finish(capture)


def archive_on_shutdown(app, collector, destination):
    """Export during ASGI cleanup, before Uvicorn re-raises a terminating signal."""
    lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def archive_lifespan(application):
        try:
            async with lifespan(application) as state:
                yield state
        finally:
            await asyncio.sleep(0)  # Flush deferred driver callbacks before copying their entries.
            destination.write_text(json.dumps(collector.snapshot(), indent=2))

    app.router.lifespan_context = archive_lifespan


def traced_query(method):
    @functools.wraps(method)
    async def call(self, query, *args, **kwargs):
        capture = current_capture.get()
        if capture is None or capture.closed:
            return await method(self, query, *args, **kwargs)
        entry = capture.begin_query(query)
        # Set even when full so a later callback cannot attach to an enclosing operation.
        token = current_query.set(entry)
        started = perf_counter()
        error = True
        try:
            value = await method(self, query, *args, **kwargs)
            error = False
            return value
        finally:
            finished = perf_counter()
            if entry is not None:
                entry.update(wall_seconds=finished - started,
                             end_offset_seconds=finished - capture.started, error=error)
            current_query.reset(token)
    return call


def driver_query_observed(record):
    entry = current_query.get()
    if entry is not None:
        entry["driver_callback_count"] += 1
        entry["driver_seconds"] = (entry["driver_seconds"] or 0) + record.elapsed


class TracedConnection(asyncpg.Connection):
    fetch = traced_query(asyncpg.Connection.fetch)
    fetchrow = traced_query(asyncpg.Connection.fetchrow)
    fetchval = traced_query(asyncpg.Connection.fetchval)
    execute = traced_query(asyncpg.Connection.execute)
    executemany = traced_query(asyncpg.Connection.executemany)
