"""Bounded process-local stage histograms; labels never include tenant or request IDs."""

import asyncio
import functools
import inspect
from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass, field
from time import perf_counter

STAGES = frozenset({
    "json_parse", "normalize", "authentication", "token_estimation", "routing", "db_pool_wait",
    "client_quota", "provider_quota", "client_reservation", "budget_reservation",
    "budget_settlement", "client_settlement", "request_persistence", "attempt_persistence",
    "usage_persistence", "routing_persistence", "request_total", "request_upload",
    "event_loop_lag", "durable_reservation", "durable_settlement", "upstream_headers",
    "gateway_pre_dispatch",
})
BUCKETS = (.001, .005, .01, .025, .05, .1, .25, .5, 1., 5., 30.)
current_trace: ContextVar[list | None] = ContextVar("metric_trace", default=None)
request_started: ContextVar[float | None] = ContextVar("metric_request_started", default=None)
TRACE_LIMIT = 64


@dataclass
class Histogram:
    count: int = 0
    errors: int = 0
    total_seconds: float = 0
    maximum_seconds: float = 0
    buckets: list[int] = field(default_factory=lambda: [0] * len(BUCKETS))

    def observe(self, elapsed: float, error: bool):
        self.count += 1
        self.errors += int(error)
        self.total_seconds += elapsed
        self.maximum_seconds = max(self.maximum_seconds, elapsed)
        for index, bound in enumerate(BUCKETS):
            if elapsed <= bound:
                self.buckets[index] += 1


class StageMetrics:
    def __init__(self):
        self.histograms = {stage: Histogram() for stage in STAGES}
        self.traces: deque[dict] = deque(maxlen=32)

    def observe(self, stage: str, elapsed: float, error: bool = False):
        self.histograms[stage].observe(elapsed, error)
        trace = current_trace.get()
        if trace is not None and len(trace) < TRACE_LIMIT:
            trace.append({"stage": stage, "seconds": elapsed, "error": error})

    def snapshot(self):
        return {
            "scope": "process", "unit": "seconds", "bucket_bounds": BUCKETS,
            "stages": {
                stage: {"count": value.count, "errors": value.errors,
                        "sum": value.total_seconds, "max": value.maximum_seconds,
                        "buckets": list(value.buckets)}
                for stage, value in sorted(self.histograms.items())
            },
        }


metrics = StageMetrics()


def measured(stage: str):
    if stage not in STAGES:
        raise ValueError("Unknown measurement stage")

    def decorate(function):
        if inspect.iscoroutinefunction(function):
            @functools.wraps(function)
            async def asynchronous(*args, **kwargs):
                started, error = perf_counter(), True
                try:
                    value = await function(*args, **kwargs)
                    error = False
                    return value
                finally:
                    metrics.observe(stage, perf_counter() - started, error)
            return asynchronous

        @functools.wraps(function)
        def synchronous(*args, **kwargs):
            started, error = perf_counter(), True
            try:
                value = function(*args, **kwargs)
                error = False
                return value
            finally:
                metrics.observe(stage, perf_counter() - started, error)
        return synchronous
    return decorate


@measured("json_parse")
async def read_json(request):
    return await request.json()


async def sample_event_loop():
    while True:
        before = perf_counter()
        await asyncio.sleep(.1)
        metrics.observe("event_loop_lag", max(0, perf_counter() - before - .1))
