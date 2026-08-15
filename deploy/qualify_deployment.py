import argparse
import asyncio
import json
import time

import httpx


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--duration-seconds", type=int, default=120)
    parser.add_argument("--interval-seconds", type=float, default=0.5)
    parser.add_argument("--max-failures", type=int, default=2)
    args = parser.parse_args()
    started = time.monotonic()
    statuses: list[int | str] = []
    async with httpx.AsyncClient(timeout=5) as client:
        while time.monotonic() - started < args.duration_seconds:
            try:
                response = await client.get(f"{args.url.rstrip('/')}/ready")
                statuses.append(response.status_code)
            except httpx.HTTPError as exc:
                statuses.append(type(exc).__name__)
            await asyncio.sleep(args.interval_seconds)
    failures = sum(status != 200 for status in statuses)
    print(
        json.dumps(
            {
                "samples": len(statuses),
                "failures": failures,
                "successful": len(statuses) - failures,
            },
            indent=2,
        )
    )
    if failures > args.max_failures:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
