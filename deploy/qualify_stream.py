import argparse
import asyncio
import json
import os
import time

import httpx


async def consume_stream(
    url: str,
    key: str,
    model: str,
    duration_seconds: int,
    slow_consumer_seconds: float,
) -> dict[str, object]:
    started = time.monotonic()
    chunks = 0
    first_chunk_ms = None
    async with httpx.AsyncClient(timeout=None) as client, client.stream(
        "POST",
        f"{url.rstrip('/')}/v1/chat/completions",
        headers={"authorization": f"Bearer {key}"},
        json={
            "model": model,
            "stream": True,
            "messages": [{"role": "user", "content": "Reply with a short status."}],
        },
    ) as response:
        response.raise_for_status()
        async for _chunk in response.aiter_bytes():
            if first_chunk_ms is None:
                first_chunk_ms = round((time.monotonic() - started) * 1000, 3)
            chunks += 1
            if slow_consumer_seconds:
                await asyncio.sleep(slow_consumer_seconds)
            if time.monotonic() - started >= duration_seconds:
                break
    return {
        "duration_seconds": round(time.monotonic() - started, 3),
        "chunks": chunks,
        "first_chunk_ms": first_chunk_ms,
        "cancelled_after_target": True,
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--duration-seconds", type=int, default=900)
    parser.add_argument("--slow-consumer-seconds", type=float, default=0.25)
    args = parser.parse_args()
    key = os.environ.get("GATEWAY_SMOKE_KEY")
    if not key:
        raise SystemExit("GATEWAY_SMOKE_KEY is required")
    result = await consume_stream(
        args.url,
        key,
        args.model,
        args.duration_seconds,
        args.slow_consumer_seconds,
    )
    print(json.dumps(result, indent=2))
    if result["chunks"] < 1 or result["duration_seconds"] < args.duration_seconds:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
