from copy import deepcopy

import pytest

from scripts.qualification_report import analyze_stability, validate_functional_report

MIB = 1024**2


@pytest.fixture
def report():
    return {
        "phases": {"mixed_soak": {
            "responses": {"200": 100, "429": 3, "transport_error": 0},
            "workflow_submissions": 4,
            "workflow_responses": {"202": 4, "429": 2, "409": 1},
            "workflow_statuses": [{"status": "completed", "count": 4}],
            "successful_reservations_settled": True,
            "provider": {"active": 0, "peak": 8},
            "bucket_bounds_seconds": [0.05, 0.1, 0.25],
            "stage_deltas": [
                {"gateway_pre_dispatch": {"count": 80, "buckets": [40, 80, 80]}},
                {"gateway_pre_dispatch": {"count": 20, "buckets": [5, 15, 20]}},
            ],
        }},
        "memory_samples_bytes": [500 * MIB] * 300 + [100 * MIB] * 600,
    }


def test_valid_report_and_sparse_counters(report):
    assert validate_functional_report(report) is None
    del report["phases"]["mixed_soak"]["responses"]["transport_error"]
    del report["phases"]["mixed_soak"]["workflow_responses"]
    assert validate_functional_report(report) is None


@pytest.mark.parametrize("partial", [None, [], {}, {"qualified": True}, {"phases": {}},
                                      {"phases": {"mixed_soak": {}}}])
def test_partial_reports_do_not_pass(partial):
    with pytest.raises(ValueError):
        validate_functional_report(partial)
    assert analyze_stability(partial)["status"] == "OPEN"


@pytest.mark.parametrize("field", [
    "responses", "workflow_submissions", "workflow_statuses",
    "successful_reservations_settled", "provider",
])
def test_missing_required_evidence(report, field):
    del report["phases"]["mixed_soak"][field]
    with pytest.raises(ValueError, match=field):
        validate_functional_report(report)


@pytest.mark.parametrize(("field", "value", "message"), [
    ("responses", {}, "successful interactive traffic"),
    ("responses", {"429": 100}, "successful interactive traffic"),
    ("responses", {"200": 1, "500": 1}, "unexpected response status"),
    ("responses", {"200": 1, "201": 0}, "unexpected response status"),
    ("responses", {"200": 1, "transport_error": 1}, "transport_error must be zero"),
    ("responses", {"200": -1}, "nonnegative integer"),
    ("responses", {"200": True}, "nonnegative integer"),
    ("responses", {"200": "100"}, "nonnegative integer"),
    ("workflow_submissions", 0, "at least one accepted workflow"),
    ("workflow_submissions", True, "nonnegative integer"),
    ("workflow_responses", {"202": 3}, "must equal accepted"),
    ("workflow_responses", {"202": 4, "500": 1}, "unexpected response status"),
    ("workflow_responses", {"202": 4, "transport_error": 1}, "transport_error"),
    ("workflow_statuses", [], "nonempty list"),
    ("workflow_statuses", [{"status": "completed", "count": 3}], "sum 3.*4"),
    ("workflow_statuses", [{"status": "completed", "count": 5}], "sum 5.*4"),
    ("workflow_statuses", [{"status": "completed", "count": 0}], "sum 0.*4"),
    ("workflow_statuses", [{"status": "failed", "count": 4}], "must be completed"),
    ("workflow_statuses", [{"status": "queued", "count": 4}], "must be completed"),
    ("workflow_statuses", [{"status": "completed"}], "nonnegative integer"),
    ("workflow_statuses", [{"status": "completed", "count": 2}] * 2, "duplicates"),
    ("successful_reservations_settled", 1, "must be true"),
    ("successful_reservations_settled", False, "must be true"),
    ("provider", {"active": 1, "peak": 8}, "active must be zero"),
    ("provider", {"active": 0, "peak": 9}, "peak must be <= 8"),
    ("provider", {"active": 0}, "provider.peak"),
    ("provider", {"active": False, "peak": 8}, "provider.active"),
])
def test_invalid_functional_evidence(report, field, value, message):
    report["phases"]["mixed_soak"][field] = value
    with pytest.raises(ValueError, match=message):
        validate_functional_report(report)


def test_empty_workload_is_not_vacuously_successful(report):
    soak = report["phases"]["mixed_soak"]
    soak.update(workflow_submissions=0, workflow_responses={}, workflow_statuses=[])
    report["qualified"] = True
    with pytest.raises(ValueError, match="at least one accepted workflow"):
        validate_functional_report(report)


def test_stability_uses_post_warmup_medians_and_records_config(report):
    result = analyze_stability(report)
    assert result["status"] == "PASS"
    assert result["memory"]["first_window_median_bytes"] == 100 * MIB
    assert result["memory"]["last_window_median_bytes"] == 100 * MIB
    assert result["memory"]["first_window_indices"] == [300, 600]
    assert result["memory"]["last_window_indices"] == [600, 900]
    assert result["config"]["max_growth_fraction"] == 0.1
    assert result["config"]["max_growth_bytes"] == 32 * MIB
    assert result["config"]["sample_interval_seconds"] == 1
    assert result["limitations"]


@pytest.mark.parametrize(("first", "last", "expected"), [
    (100 * MIB, 110 * MIB, "PASS"),  # Inclusive 10% limit.
    (100 * MIB, 110 * MIB + 1, "FAIL"),  # Fraction fails, bytes pass.
    (400 * MIB, 432 * MIB, "PASS"),  # Inclusive 32 MiB limit.
    (400 * MIB, 432 * MIB + 1, "FAIL"),  # Bytes fail, fraction passes.
    (100 * MIB, 90 * MIB, "PASS"),
])
def test_memory_requires_both_growth_limits(report, first, last, expected):
    report["memory_samples_bytes"] = [MIB] * 300 + [first] * 300 + [last] * 300
    assert analyze_stability(report)["memory"]["status"] == expected
    assert analyze_stability(report)["status"] == expected


def test_memory_custom_thresholds(report):
    report["memory_samples_bytes"][-300:] = [120 * MIB] * 300
    result = analyze_stability(report, max_growth_fraction=0.2, max_growth_bytes=20 * MIB)
    assert result["status"] == "PASS"
    assert result["config"]["max_growth_fraction"] == 0.2
    assert result["config"]["max_growth_bytes"] == 20 * MIB


def test_long_run_uses_last_window_and_resists_outliers(report):
    report["memory_samples_bytes"] += [100 * MIB] * 6300
    report["memory_samples_bytes"][300] = 10000 * MIB
    report["memory_samples_bytes"][-1] = 10000 * MIB
    result = analyze_stability(report)["memory"]
    assert result["status"] == "PASS"
    assert result["growth_bytes"] == 0
    assert result["last_window_indices"] == [6900, 7200]


@pytest.mark.parametrize("samples", [None, [], [MIB] * 899, "1000000",
                                      [0] * 900, [float("nan")] * 900,
                                      [float("inf")] * 900, [-1] * 900, [True] * 900])
def test_missing_insufficient_or_invalid_memory_is_open(report, samples):
    report["memory_samples_bytes"] = samples
    report["phases"]["mixed_soak"].update(rss_first_bytes=MIB, rss_last_bytes=MIB)
    result = analyze_stability(report)
    assert result["memory"]["status"] == "OPEN"
    assert result["status"] == "OPEN"


def test_missing_memory_is_open(report):
    del report["memory_samples_bytes"]
    assert analyze_stability(report)["memory"]["status"] == "OPEN"


@pytest.mark.parametrize("config", [
    {"max_growth_fraction": -0.1}, {"max_growth_fraction": float("nan")},
    {"max_growth_fraction": True}, {"max_growth_bytes": -1}, {"max_growth_bytes": 1.5},
])
def test_invalid_configuration_raises(report, config):
    with pytest.raises(ValueError):
        analyze_stability(report, **config)


def test_predispatch_aggregates_counts_not_process_percentiles_or_means(report):
    result = analyze_stability(report)["predispatch"]
    assert result["status"] == "PASS"
    assert result["count"] == 100
    assert result["cumulative_bucket_counts"] == [45, 95, 100]
    assert result["count_at_or_below_target"] == 95
    assert result["p95_rank"] == 95
    assert result["bounded"] is True
    assert result["p95_interval_seconds"] == {
        "lower": 0.05, "upper": 0.1, "lower_inclusive": False, "upper_inclusive": True,
    }
    assert result["target_inequality"] == "p95 <= 0.1 seconds"


def test_predispatch_just_below_required_count_fails_despite_low_mean(report):
    stage = report["phases"]["mixed_soak"]["stage_deltas"][1]["gateway_pre_dispatch"]
    stage.update(buckets=[5, 14, 20], sum=0.001)
    result = analyze_stability(report)
    assert result["status"] == "FAIL"
    assert result["predispatch"]["p95_interval_seconds"]["upper"] == 0.25
    assert result["predispatch"]["target_inequality"] == "p95 > 0.1 seconds"


@pytest.mark.parametrize(("count", "buckets", "lower", "upper", "status"), [
    (1, [1, 1, 1], 0, 0.05, "PASS"),
    (1, [0, 0, 1], 0.1, 0.25, "FAIL"),
    (100, [0, 0, 94], 0.25, None, "FAIL"),
    (21, [0, 19, 21], 0.1, 0.25, "FAIL"),  # ceil(19.95), not floor.
])
def test_histogram_intervals_and_overflow(report, count, buckets, lower, upper, status):
    report["phases"]["mixed_soak"]["stage_deltas"] = [
        {"gateway_pre_dispatch": {"count": count, "buckets": buckets}},
    ]
    result = analyze_stability(report)["predispatch"]
    assert result["status"] == status
    assert result["p95_interval_seconds"]["lower"] == lower
    assert result["p95_interval_seconds"]["upper"] == upper


@pytest.mark.parametrize("histogram", [
    {}, {"count": 0, "buckets": [0, 0, 0]}, {"count": 10, "sum": 0.1},
    {"count": 10, "buckets": [1, 2]}, {"count": 10, "buckets": [5, 4, 10]},
    {"count": 10, "buckets": [5, 10, 11]}, {"count": 10, "buckets": [-1, 0, 10]},
    {"count": True, "buckets": [1, 1, 1]}, {"count": 10, "buckets": [1, 2.5, 10]},
])
def test_invalid_histograms_are_open(report, histogram):
    report["phases"]["mixed_soak"]["stage_deltas"] = [{"gateway_pre_dispatch": histogram}]
    assert analyze_stability(report)["status"] == "OPEN"


@pytest.mark.parametrize("bounds", [None, [], [0.1, 0.1, 0.25], [0.1, 0.05, 0.25],
                                     [0.05, 0.1, float("inf")], [0.05, 0.2, 0.25]])
def test_missing_invalid_or_coarse_bounds_cannot_pass(report, bounds):
    report["phases"]["mixed_soak"]["bucket_bounds_seconds"] = bounds
    assert analyze_stability(report)["predispatch"]["status"] == "OPEN"


def test_missing_process_histogram_cannot_pass(report):
    report["phases"]["mixed_soak"]["stage_deltas"].append({})
    assert analyze_stability(report)["status"] == "OPEN"


def test_no_histogram_never_infers_p95_from_other_latency_or_means(report):
    soak = report["phases"]["mixed_soak"]
    soak.update(stage_deltas=[], p95_ms=1, latency_by_status={"200": {"p95_ms": 1}})
    result = analyze_stability(report)
    assert result["status"] == "OPEN"
    assert "p95_interval_seconds" not in result["predispatch"]


def test_fail_takes_precedence_over_missing_evidence(report):
    report["memory_samples_bytes"][-300:] = [200 * MIB] * 300
    del report["phases"]["mixed_soak"]["stage_deltas"]
    result = analyze_stability(report)
    assert result["status"] == "FAIL"
    assert result["predispatch"]["status"] == "OPEN"


def test_helpers_do_not_mutate_report(report):
    original = deepcopy(report)
    validate_functional_report(report)
    analyze_stability(report)
    assert report == original
