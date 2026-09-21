"""Pure, offline checks for the staging runner's report dictionaries."""

from collections.abc import Mapping
from math import isfinite
from statistics import median


def _mapping(value, path):
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} must be an object")
    return value


def _count(value, path):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{path} must be a nonnegative integer")
    return value


def _number(value, path, *, positive=False):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not isfinite(value)
        or value < 0
        or (positive and value == 0)
    ):
        qualifier = "positive" if positive else "nonnegative"
        raise ValueError(f"{path} must be a finite {qualifier} number")
    return value


def _soak(report):
    report = _mapping(report, "report")
    phases = _mapping(report.get("phases"), "phases")
    return _mapping(phases.get("mixed_soak"), "phases.mixed_soak")


def _responses(value, path, allowed):
    responses = _mapping(value, path)
    for status, count in responses.items():
        _count(count, f"{path}.{status}")
        if status == "transport_error":
            if count != 0:
                raise ValueError(f"{path}.transport_error must be zero; got {count}")
        elif status not in allowed:
            raise ValueError(f"{path} contains unexpected response status {status!r}")
    return responses


def validate_functional_report(report: Mapping) -> None:
    """Return None on success; raise descriptive ValueError on invalid/incomplete evidence.

    Checks phases.mixed_soak, independently of the runner's `qualified` flag.
    Absent transport_error counters mean zero (the runner uses sparse Counters).
    workflow_submissions is the accepted-job count. When workflow_responses is
    supplied, its 202 count must agree with that count as well.
    """
    soak = _soak(report)
    responses = _responses(soak.get("responses"), "responses", {"200", "429"})
    if responses.get("200", 0) == 0:
        raise ValueError("responses must contain nonempty successful interactive traffic (200 > 0)")

    accepted = _count(soak.get("workflow_submissions"), "workflow_submissions")
    if accepted == 0:
        raise ValueError("workflow_submissions must contain at least one accepted workflow")
    if "workflow_responses" in soak:
        workflow_responses = _responses(
            soak["workflow_responses"], "workflow_responses", {"202", "409", "429"},
        )
        if workflow_responses.get("202", 0) != accepted:
            raise ValueError("workflow_responses.202 must equal accepted workflow_submissions")

    statuses = soak.get("workflow_statuses")
    if not isinstance(statuses, list) or not statuses:
        raise ValueError("workflow_statuses must be a nonempty list accounting for accepted jobs")
    total = 0
    seen = set()
    for index, row in enumerate(statuses):
        path = f"workflow_statuses[{index}]"
        row = _mapping(row, path)
        status = row.get("status")
        if status != "completed":
            raise ValueError(f"{path}.status must be completed; got {status!r}")
        if status in seen:
            raise ValueError(f"{path} duplicates status {status!r}")
        seen.add(status)
        total += _count(row.get("count"), f"{path}.count")
    if total != accepted:
        raise ValueError(
            f"workflow_statuses sum {total} must equal accepted workflow_submissions {accepted}"
        )

    if soak.get("successful_reservations_settled") is not True:
        raise ValueError("successful_reservations_settled must be true")
    provider = _mapping(soak.get("provider"), "provider")
    active = _count(provider.get("active"), "provider.active")
    peak = _count(provider.get("peak"), "provider.peak")
    if active != 0:
        raise ValueError(f"provider.active must be zero; got {active}")
    if peak > 8:
        raise ValueError(f"provider.peak must be <= 8; got {peak}")
    _translated_coverage(report, soak)


def _translated_coverage(report, soak):
    if soak.get("translated_coverage_required") is not True:
        if report.get("workload_profile") == "translated":
            raise ValueError(
                "translated run must include translated_coverage_required evidence"
            )
        return
    coverage = _mapping(report.get("phases", {}).get("translated_coverage"),
                        "phases.translated_coverage")
    protocols = ("openai_chat_completions", "openai_responses", "anthropic_messages")
    required = {f"{protocol}/{scenario}_two_turn"
                for protocol in protocols
                for scenario in ("json", "sse", "workflow")}
    if set(coverage) != required:
        raise ValueError("translated run must include all nine protocol/scenario rows")
    for key, row in coverage.items():
        path = f"translated_coverage.{key}"
        row = _mapping(row, path)
        counts = {name: _count(row.get(name), f"{path}.{name}")
                  for name in ("started", "completed", "overloaded", "failed")}
        if counts["completed"] == 0 or counts["failed"] != 0 or row.get("failures") != []:
            raise ValueError(f"{path} must include completed journeys with zero failures")
        if counts["started"] != counts["completed"] + counts["overloaded"]:
            raise ValueError(f"{path} contains unfinished journeys")


def _memory(report, config):
    result = {
        "status": "OPEN",
        "limitations": [
            "Assumes memory_samples_bytes were measured once per second in chronological order; "
            "timestamps and sampling gaps cannot be verified from this array.",
            "Window medians assess aggregate RSS growth, not per-process leaks or transient peaks.",
        ],
    }
    samples = report.get("memory_samples_bytes")
    if not isinstance(samples, list):
        result["limitations"].append("memory_samples_bytes is missing or is not a list.")
        return result
    result["sample_count"] = len(samples)
    try:
        for index, sample in enumerate(samples):
            _number(sample, f"memory_samples_bytes[{index}]", positive=True)
    except ValueError as error:
        result["limitations"].append(str(error))
        return result
    if len(samples) < config["minimum_samples"]:
        result["limitations"].append(
            "At least 900 one-second samples are required: 300 warmup + two 300-sample windows."
        )
        return result
    first = median(samples[300:600])
    last = median(samples[-300:])
    growth = last - first
    fraction = growth / first
    result.update({
        "status": "PASS" if (
            fraction <= config["max_growth_fraction"] and growth <= config["max_growth_bytes"]
        ) else "FAIL",
        "first_window_indices": [300, 600],
        "last_window_indices": [len(samples) - 300, len(samples)],
        "first_window_median_bytes": first,
        "last_window_median_bytes": last,
        "growth_bytes": growth,
        "growth_fraction": fraction,
    })
    return result


def _histogram(soak):
    bounds = soak.get("bucket_bounds_seconds")
    if not isinstance(bounds, (list, tuple)) or not bounds:
        raise ValueError("bucket_bounds_seconds must contain cumulative histogram bounds")
    previous = 0
    for bound in bounds:
        _number(bound, "bucket_bounds_seconds entry", positive=True)
        if bound <= previous:
            raise ValueError("bucket_bounds_seconds must be strictly increasing")
        previous = bound
    stages = soak.get("stage_deltas")
    if not isinstance(stages, list) or not stages:
        raise ValueError("stage_deltas must contain measured gateway_pre_dispatch histograms")
    total, aggregate = 0, [0] * len(bounds)
    for index, stage in enumerate(stages):
        path = f"stage_deltas[{index}].gateway_pre_dispatch"
        stage = _mapping(stage, f"stage_deltas[{index}]")
        histogram = _mapping(stage.get("gateway_pre_dispatch"), path)
        count = _count(histogram.get("count"), f"{path}.count")
        buckets = histogram.get("buckets")
        if not isinstance(buckets, (list, tuple)) or len(buckets) != len(bounds):
            raise ValueError(f"{path}.buckets must match bucket_bounds_seconds length")
        previous = 0
        for bucket_index, bucket in enumerate(buckets):
            _count(bucket, f"{path}.buckets[{bucket_index}]")
            if not previous <= bucket <= count:
                raise ValueError(f"{path}.buckets must be cumulative and <= count")
            aggregate[bucket_index] += bucket
            previous = bucket
        total += count
    if total == 0:
        raise ValueError("gateway_pre_dispatch has no observations")
    return bounds, aggregate, total


def _predispatch(report):
    result = {
        "status": "OPEN",
        "target_seconds": 0.1,
        "limitations": [
            "p95 is a bounded histogram inequality, not an exact latency "
            "or an estimate from means.",
            "Aggregate stage histograms do not establish per-process "
            "or time-window latency stability.",
        ],
    }
    try:
        bounds, buckets, count = _histogram(_soak(report))
    except ValueError as error:
        result["limitations"].append(str(error))
        return result

    rank = (95 * count + 99) // 100
    index = next((i for i, bucket in enumerate(buckets) if bucket >= rank), len(bounds))
    lower = bounds[index - 1] if index else 0
    upper = bounds[index] if index < len(bounds) else None
    result.update({
        "count": count,
        "p95_rank": rank,
        "bucket_bounds_seconds": list(bounds),
        "cumulative_bucket_counts": buckets,
        "p95_interval_seconds": {
            "lower": lower, "upper": upper,
            "lower_inclusive": index == 0, "upper_inclusive": upper is not None,
        },
        "bounded": True,
        "p95_inequality": (
            f"{lower:g} {'<=' if index == 0 else '<'} p95 <= {upper:g} seconds"
            if upper is not None else f"p95 > {lower:g} seconds"
        ),
    })
    if 0.1 not in bounds:
        result["limitations"].append(
            "The exact <= 0.1 second bucket is missing; a coarser bucket cannot certify the target."
        )
        return result
    below = buckets[bounds.index(0.1)]
    result.update({
        "status": "PASS" if below >= rank else "FAIL",
        "count_at_or_below_target": below,
        "fraction_at_or_below_target": below / count,
        "target_inequality": "p95 <= 0.1 seconds" if below >= rank else "p95 > 0.1 seconds",
    })
    return result


def analyze_stability(
    report: Mapping, *, max_growth_fraction: float = 0.10, max_growth_bytes: int = 32 * 1024**2,
) -> dict:
    """Analyze memory growth and aggregate predispatch p95 without performing any I/O.

    Returns status, config, memory, predispatch, limitations. PASS requires both
    measured checks to pass; any FAIL wins, otherwise missing/invalid evidence is
    OPEN. Functional qualification remains a separate validate_functional_report
    call. Invalid threshold configuration raises ValueError. Window indices are
    zero-based and end-exclusive; an unbounded histogram upper limit is None.
    """
    _number(max_growth_fraction, "max_growth_fraction")
    _count(max_growth_bytes, "max_growth_bytes")
    config = {
        "sample_interval_seconds": 1,
        "warmup_seconds": 300,
        "window_seconds": 300,
        "minimum_samples": 900,
        "max_growth_fraction": max_growth_fraction,
        "max_growth_bytes": max_growth_bytes,
        "growth_criterion": "growth_fraction <= max_growth_fraction AND "
                            "growth_bytes <= max_growth_bytes",
        "predispatch_p95_target_seconds": 0.1,
    }
    if not isinstance(report, Mapping):
        report = {}
    memory = _memory(report, config)
    predispatch = _predispatch(report)
    statuses = {memory["status"], predispatch["status"]}
    status = "FAIL" if "FAIL" in statuses else "OPEN" if "OPEN" in statuses else "PASS"
    return {
        "status": status,
        "config": config,
        "memory": memory,
        "predispatch": predispatch,
        "limitations": [
            "Stability results do not replace functional report validation.",
            *memory["limitations"], *predispatch["limitations"],
        ],
    }
