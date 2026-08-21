"""Measure cost for an OpenAI-protocol model the same way as the Anthropic one.

Reads the provider billing counter, sends one Chat Completions request, reads the
counter again. Two samples with opposite input:output ratios determine the separated
rates; a third at a different ratio validates them out of sample.

Refuses to persist a sample that a concurrent gateway request could have inflated.
"""

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import UTC, datetime

import httpx

sys.path.insert(0, "/root/ai-gateway")
sys.path.insert(0, "/root/ai-gateway/deploy")

from measure_cost import concurrent_gateway_traffic, credential, read_counter  # noqa: E402


async def send(
    secret: str,
    base_url: str,
    model: str,
    prompt: str,
    max_tokens: int,
    settings: dict | None = None,
) -> dict:
    started = time.perf_counter()
    settings = settings or {}
    # Some relays reject a request that does not carry the client headers the
    # provider is configured with, exactly as the gateway sends them.
    headers = {
        "authorization": f"Bearer {secret}",
        "content-type": "application/json",
        **(settings.get("default_headers") or {}),
    }
    url = f"{base_url.rstrip('/')}/v1/chat/completions"
    query = settings.get("endpoint_query") or {}
    if query:
        url += "?" + "&".join(f"{k}={v}" for k, v in query.items())
    async with httpx.AsyncClient(timeout=180) as client:
        response = await client.post(
            url,
            headers=headers,
            json={
                "model": model,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
    latency = (time.perf_counter() - started) * 1000
    if response.status_code != 200:
        raise SystemExit(f"upstream returned {response.status_code}: {response.text[:300]}")
    payload = response.json()
    usage = payload.get("usage") or {}
    details = usage.get("prompt_tokens_details") or {}
    return {
        "model": payload.get("model"),
        "input_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("completion_tokens"),
        "cached_tokens": details.get("cached_tokens"),
        "latency_ms": round(latency, 1),
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--max-tokens", type=int, default=32)
    parser.add_argument("--label", default="")
    parser.add_argument("--persist", action="store_true")
    args = parser.parse_args()

    secret, base_url, provider_id, credential_id, settings = await credential(args.provider)
    window = datetime.now(UTC)
    before = await read_counter(base_url, secret)
    if before is None:
        raise SystemExit(f"{args.provider} does not expose a billing counter")
    result = await send(
        secret, base_url, args.model, args.prompt, args.max_tokens, settings
    )
    await asyncio.sleep(5)
    after = await read_counter(base_url, secret)
    contaminating = await concurrent_gateway_traffic(credential_id, window)

    raw = round(after - before, 8)
    measured = round(raw / 100, 10)
    total = (result["input_tokens"] or 0) + (result["output_tokens"] or 0)
    print(
        json.dumps(
            {
                "label": args.label,
                "provider": args.provider,
                "requested_model": args.model,
                "response_model": result["model"],
                **{k: result[k] for k in ("input_tokens", "output_tokens", "cached_tokens")},
                "raw_delta": raw,
                "measured_cost": measured,
                "blended_per_million": (
                    round(measured / total * 1_000_000, 6) if total else None
                ),
                "concurrent_gateway_attempts": contaminating,
                "latency_ms": result["latency_ms"],
            },
            indent=2,
        )
    )
    if contaminating:
        raise SystemExit(
            f"REFUSING TO PERSIST: {contaminating} gateway attempt(s) shared this credential"
        )
    if args.persist:
        from gateway.configuration import create_pool

        pool = await create_pool(os.environ["GATEWAY_DATABASE_URL"])
        try:
            await pool.execute(
                """
                insert into public.cost_samples(provider_id,provider_name_snapshot,model_id,
                  upstream_model,method,usage_before,usage_after,raw_delta,scale,
                  measured_cost,currency,input_tokens,output_tokens,cached_tokens,note)
                values($1,$2,$3,$4,'billing_usage_delta',$5,$6,$7,'cent',$8,'USD',$9,$10,$11,$12)
                """,
                provider_id,
                args.provider,
                args.model,
                result["model"],
                before,
                after,
                raw,
                measured,
                result["input_tokens"],
                result["output_tokens"],
                result["cached_tokens"],
                f"OpenAI-protocol calibration: {args.label}",
            )
            print("persisted to cost_samples")
        finally:
            await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
