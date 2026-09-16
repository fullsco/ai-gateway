import json
from types import SimpleNamespace

import pytest

from scripts.benchmark_admission import distribution, parse_args, run_case
from scripts.benchmark_fixtures import PROTOCOLS, SCENARIOS, payload


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_synthetic_payload_has_exact_size_and_valid_envelope(protocol):
    for size in (256, 8192):
        body = payload(protocol, "stream", size)
        assert len(body) == size
        data = json.loads(body)
        assert data["stream"] is True
        assert data["model"] == "synthetic"
        assert data.get("input") or data.get("messages")


def test_distribution_preserves_tail_and_empty_cases():
    assert distribution([])["p99"] is None
    assert distribution([1, 2, 3, 100])["p99"] > 97
    assert distribution([2])["p95"] == 2


@pytest.mark.parametrize("args", [
    ["--waves", "999"], ["--concurrency", "999"], ["--sizes", "999999999"],
    ["--delay", "nan"], ["--body-budget", "999999999"],
])
def test_unsafe_benchmark_dimensions_are_rejected(args):
    with pytest.raises(SystemExit):
        parse_args(["--output", "/tmp/opencode/unused.json", *args])


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("scenario", SCENARIOS)
async def test_mock_workloads_qualify_and_release_all_resources(protocol, scenario):
    options = SimpleNamespace(
        capacity=2, max_body=1024, body_budget=2048,
        upload_timeout=.05 if scenario == "upload_timeout" else 1,
        delay=.001, waves=1, mode="burst",
    )
    result = await run_case(options, protocol, scenario, 256, 8)
    assert result["qualified"], result
    assert result["requests"] == 8
    assert result["admission_drained"]
    assert result["streams_opened"] == result["streams_closed"]
    if scenario == "oversize":
        assert result["status_counts"] == {"413": 8}
    else:
        assert result["status_counts"].get("429", 0) > 0
    if scenario == "healthy":
        assert result["completed"] == 2
