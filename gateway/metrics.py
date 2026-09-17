"""Bounded process-local stage histograms; labels never include tenant or request IDs."""

import asyncio
import functools
import inspect
from collections import deque
from collections.abc import Callable
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from time import perf_counter

STAGES = frozenset({
    "json_parse", "normalize", "authentication", "token_estimation", "routing", "db_pool_wait",
    "client_quota", "provider_quota", "client_reservation", "budget_reservation",
    "budget_settlement", "client_settlement", "request_persistence", "attempt_persistence",
    "usage_persistence", "routing_persistence", "request_total", "request_upload",
    "event_loop_lag", "durable_reservation", "durable_settlement", "upstream_headers",
    "gateway_pre_dispatch", "capacity_admission", "route_capacity_admission",
    "shared_circuit_allow", "route_concurrency_acquire", "ownership_verify",
    "durable_dispatch",
})
BUCKETS = (.001, .005, .01, .025, .05, .1, .25, .5, 1., 5., 30.)
current_trace: ContextVar[list | None] = ContextVar("metric_trace", default=None)
request_started: ContextVar[float | None] = ContextVar("metric_request_started", default=None)
TRACE_LIMIT = 64


@dataclass(frozen=True)
class ActiveMeasurement:
    stage: str
    started: float
    task: asyncio.Task | None


# Optional diagnostic observer; production histograms and trace payloads stay bounded.
measurement_observer: ContextVar[Callable[[str, float, bool], None] | None] = ContextVar(
    "measurement_observer", default=None,
)
active_measurement: ContextVar[ActiveMeasurement | None] = ContextVar(
    "active_measurement", default=None,
)


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
        observer = measurement_observer.get()
        if observer is not None:
            # An optional diagnostic must not alter inference results or mask its error.
            with suppress(Exception):
                observer(stage, elapsed, error)

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


@contextmanager
def timed(stage: str):
    if stage not in STAGES:
        raise ValueError("Unknown measurement stage")
    started = perf_counter()
    token = None
    if measurement_observer.get() is not None:
        try:
            task = asyncio.current_task()
        except RuntimeError:
            task = None
        token = active_measurement.set(ActiveMeasurement(stage, started, task))
    error = True
    try:
        yield
        error = False
    finally:
        try:
            metrics.observe(stage, perf_counter() - started, error)
        finally:
            if token is not None:
                active_measurement.reset(token)


def measured(stage: str):
    if stage not in STAGES:
        raise ValueError("Unknown measurement stage")

    def decorate(function):
        if inspect.iscoroutinefunction(function):
            @functools.wraps(function)
            async def asynchronous(*args, **kwargs):
                with timed(stage):
                    return await function(*args, **kwargs)
            return asynchronous

        @functools.wraps(function)
        def synchronous(*args, **kwargs):
            with timed(stage):
                return function(*args, **kwargs)
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
