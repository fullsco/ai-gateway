"""Measure the cache-read rate by billing the same prompt twice.

The separated input/output rates were solved from samples with no cache reads, so
they say nothing about what a cached token costs. Real traffic is almost entirely
cache reads, and estimate_cost defaults an unconfigured cached rate to the full
input rate, so the wrong answer here overstates production cost by roughly the
cache discount.

Request 1 writes the cache, request 2 reads it. Each is billed separately against
the provider counter, so the second request isolates the cache-read rate.
"""

import asyncio
import json
import sys

import anthropic

sys.path.insert(0, "/root/ai-gateway")
sys.path.insert(0, "/root/ai-gateway/deploy")

from measure_cost import concurrent_gateway_traffic, credential, read_counter  # noqa: E402

MODEL = "claude-opus-5"
INPUT_RATE = 2.0 / 1_000_000
OUTPUT_RATE = 10.0 / 1_000_000


def cached_request(secret: str, base_url: str, body: str) -> dict:
    client = anthropic.Anthropic(
        api_key=secret, base_url=base_url, max_retries=0, timeout=180
    )
    message = client.messages.create(
        model=MODEL,
        max_tokens=8,
        system=[
            {
                "type": "text",
                "text": body,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": "Reply with exactly: OK"}],
    )
    usage = message.usage
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read": getattr(usage, "cache_read_input_tokens", 0) or 0,
        "cache_write": getattr(usage, "cache_creation_input_tokens", 0) or 0,
    }


async def main() -> None:
    from datetime import UTC, datetime

    secret, base_url, _, credential_id, _ = await credential("AgentRouter")
    # A body large enough to be cacheable; Anthropic requires a minimum length.
    body = "Reference material for cache calibration. " + (
        " ".join(f"Section {i} records lot {i * 7} in aisle {i % 13}." for i in range(700))
    )

    results = []
    for label in ("write", "read"):
        window = datetime.now(UTC)
        before = await read_counter(base_url, secret)
        usage = cached_request(secret, base_url, body)
        await asyncio.sleep(5)
        after = await read_counter(base_url, secret)
        contaminated = await concurrent_gateway_traffic(credential_id, window)
        raw = round(after - before, 8)
        results.append(
            {
                "phase": label,
                **usage,
                "raw_delta": raw,
                "measured_cost": round(raw / 100, 10),
                "concurrent_gateway_attempts": contaminated,
            }
        )
        print(json.dumps(results[-1], indent=2))

    read = next(r for r in results if r["phase"] == "read")
    if read["concurrent_gateway_attempts"]:
        raise SystemExit("read phase contaminated by concurrent traffic; rerun when idle")
    if not read["cache_read"]:
        raise SystemExit("no cache read occurred; cannot isolate the cached rate")

    # cost = uncached_input*I + output*O + cache_read*C  ->  solve for C
    uncached = read["input_tokens"]
    residual = (
        read["measured_cost"] - uncached * INPUT_RATE - read["output_tokens"] * OUTPUT_RATE
    )
    cached_rate = residual / read["cache_read"] * 1_000_000
    print("\n=== cache-read rate ===")
    print(f"  cache_read tokens      : {read['cache_read']:,}")
    print(f"  uncached input tokens  : {uncached:,}")
    print(f"  measured cost          : ${read['measured_cost']:.8f}")
    print(f"  implied cached rate    : ${cached_rate:.6f} per 1M")
    print(f"  vs input rate          : ${INPUT_RATE * 1_000_000:.2f} per 1M")
    if cached_rate > 0:
        print(f"  discount factor        : {INPUT_RATE * 1_000_000 / cached_rate:.2f}x cheaper")


if __name__ == "__main__":
    asyncio.run(main())
