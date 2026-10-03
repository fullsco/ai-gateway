"""Finite low-memory mock soak, with mixed protocol/stream scenarios and cleanup assertions."""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from time import monotonic
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.benchmark_admission import bootstrap, metadata, rss_bytes, run_case  # noqa: E402


async def soak(seconds):
    options = SimpleNamespace(capacity=4, max_body=1048576, body_budget=4194304,
                              upload_timeout=1, delay=.005, waves=2, mode="closed-loop")
    started = monotonic()
    samples, completed, requests, batches = [], 0, 0, 0
    scenarios = ("healthy", "slow_stream", "disconnect", "slow_reader")
    protocols = ("chat_native", "messages_native", "responses_native", "messages_via_chat")
    while monotonic() - started < seconds:
        # One batch at a time; at most eight request tasks and four admitted requests.
        case = await run_case(options, protocols[batches % 4], scenarios[(batches // 4) % 4],
                              8192 if batches % 2 else 262144, 8)
        if not case["qualified"]:
            raise RuntimeError(f"Mock soak case failed: {case['protocol']}/{case['scenario']}")
        requests += case["requests"]
        completed += case["completed"]
        batches += 1
        if batches % 16 == 0:
            samples.append({"seconds": monotonic() - started, "rss_bytes": rss_bytes(),
                            "requests": requests})
        await asyncio.sleep(.01)
    return {"kind": "bounded_mock_soak", "metadata": metadata(), "batches": batches,
            "requests": requests, "completed": completed, "seconds": monotonic() - started,
            "qualified": True, "rss_samples": samples,
            "limitations": ["in-process mocked provider", "no DB or network throughput claim",
                            "short resource-bounded soak, not production-duration validation"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=60, choices=range(10, 121))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    target = args.output.resolve()
    if not target.is_relative_to("/tmp/opencode") or target.exists() or not target.parent.is_dir():
        parser.error("Choose a fresh file under an existing /tmp/opencode directory")
    bootstrap()
    result = asyncio.run(soak(args.seconds))
    with target.open("x") as stream:
        json.dump(result, stream, indent=2)
    print(json.dumps({key: result[key] for key in ("qualified", "batches", "requests", "seconds")}))


if __name__ == "__main__":
    main()
