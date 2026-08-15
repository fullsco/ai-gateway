import argparse
import asyncio
import json
import socket
import ssl
import time

import httpx


async def check_http(client: httpx.AsyncClient, base_url: str) -> dict[str, object]:
    health = await client.get(f"{base_url}/health")
    ready = await client.get(f"{base_url}/ready")
    version = await client.get(f"{base_url}/version")
    return {
        "health": health.status_code,
        "ready": ready.status_code,
        "version": version.status_code,
        "request_id": bool(ready.headers.get("x-request-id", "").startswith("gw_")),
    }


def check_tls(hostname: str) -> dict[str, object]:
    context = ssl.create_default_context()
    with socket.create_connection((hostname, 443), timeout=10) as raw, context.wrap_socket(
        raw, server_hostname=hostname
    ) as tls:
        protocol = tls.version()
        certificate = tls.getpeercert()
    with httpx.Client(verify=context, trust_env=False, timeout=10) as client:
        response = client.get(f"https://{hostname}/ready")
        return {
            "status": response.status_code,
            "verified": True,
            "protocol": protocol,
            "certificate_subject": certificate.get("subject", []),
        }


async def check_concurrency(client: httpx.AsyncClient, base_url: str, count: int) -> list[int]:
    responses = await asyncio.gather(*(client.get(f"{base_url}/ready") for _ in range(count)))
    return [response.status_code for response in responses]


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--local", default="http://127.0.0.1:8320")
    parser.add_argument("--public-host", default="api.duedirect.info")
    parser.add_argument("--skip-public", action="store_true")
    parser.add_argument("--concurrency", type=int, default=20)
    args = parser.parse_args()

    started = time.monotonic()
    async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
        local, concurrent = await asyncio.gather(
            check_http(client, args.local),
            check_concurrency(client, args.local, args.concurrency),
        )
        public = None
        tls = None
        if not args.skip_public:
            public = await check_http(client, f"https://{args.public_host}")
            tls = check_tls(args.public_host)
    result = {
        "local": local,
        "public": public,
        "tls": tls,
        "concurrency": {
            "requests": len(concurrent),
            "successful": sum(status == 200 for status in concurrent),
        },
        "duration_ms": round((time.monotonic() - started) * 1000, 3),
    }
    print(json.dumps(result, indent=2))
    if local["ready"] != 200:
        raise SystemExit(1)
    if not args.skip_public and (public["ready"] != 200 or tls["status"] != 200):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
