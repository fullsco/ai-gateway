"""Measure real cost by reading the provider usage counter around one request.

Replicates the operator's method: read /v1/dashboard/billing/usage, send one
request through the official Anthropic SDK, read the counter again. The raw
before/after values are retained so the cent-scale convention can be confirmed
rather than assumed, and so input and output rates can later be separated.
"""

import argparse
import asyncio
import json
import os
import sys
import time

import anthropic
import httpx

sys.path.insert(0, "/root/ai-gateway")

USAGE_PATH = "/v1/dashboard/billing/usage"


async def credential(provider: str) -> tuple[str, str, str, str, dict]:
    from dotenv import load_dotenv

    load_dotenv("/root/ai-gateway/.env")
    from gateway.configuration import create_pool
    from gateway.security import CredentialCipher, EncryptedCredential

    cipher = CredentialCipher.from_base64(os.environ["GATEWAY_CREDENTIAL_ENCRYPTION_KEY"])
    pool = await create_pool(os.environ["GATEWAY_DATABASE_URL"])
    try:
        # Prefer the credential least likely to be carrying live traffic: the
        # billing counter is per credential and cumulative, so a concurrent
        # request inflates the delta and silently corrupts the measurement.
        row = await pool.fetchrow(
            """
            select c.id::text as id, p.id::text as provider_id, p.base_url, p.settings,
                   c.secret_version, c.secret_nonce, c.secret_ciphertext
            from public.provider_credentials c
            join public.providers p on p.id = c.provider_id
            where p.name = $1 and c.enabled
            order by (c.health = 'healthy') desc,
                     coalesce(c.last_used_at, timestamptz 'epoch') asc,
                     c.priority
            limit 1
            """,
            provider,
        )
    finally:
        await pool.close()
    if row is None:
        raise SystemExit(f"no enabled credential for {provider}")
    secret = cipher.decrypt(
        EncryptedCredential(
            version=row["secret_version"],
            nonce=row["secret_nonce"],
            ciphertext=row["secret_ciphertext"],
        ),
        context=f"provider-credential:{row['id']}",
    )
    settings = row["settings"]
    if isinstance(settings, str):
        settings = json.loads(settings)
    return secret, row["base_url"], row["provider_id"], row["id"], settings or {}


async def concurrent_gateway_traffic(credential_id: str, since) -> int:
    """Gateway attempts on this credential during the measurement window.

    Any such attempt means the billing delta includes cost that is not ours, so
    the sample must be treated as contaminated rather than quietly averaged in.
    """
    from gateway.configuration import create_pool

    pool = await create_pool(os.environ["GATEWAY_DATABASE_URL"])
    try:
        return await pool.fetchval(
            """
            select count(*) from public.request_attempts
            where credential_id = $1::uuid and started_at >= $2
            """,
            credential_id,
            since,
        )
    finally:
        await pool.close()


async def read_counter(base_url: str, secret: str) -> float | None:
    """Total spend reported by the provider, or None if it does not expose it."""
    url = f"{base_url.rstrip('/')}{USAGE_PATH}"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(url, headers={"authorization": f"Bearer {secret}"})
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    if "application/json" not in response.headers.get("content-type", ""):
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    value = payload.get("total_usage") if isinstance(payload, dict) else None
    return float(value) if isinstance(value, int | float) else None


def send(secret: str, base_url: str, model: str, prompt: str, max_tokens: int) -> dict:
    client = anthropic.Anthropic(
        api_key=secret, base_url=base_url, max_retries=0, timeout=180
    )
    started = time.perf_counter()
    message = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return {
        "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        "model": message.model,
        "input_tokens": message.usage.input_tokens,
        "output_tokens": message.usage.output_tokens,
        "cached_tokens": getattr(message.usage, "cache_read_input_tokens", None),
        "text": "".join(b.text for b in message.content if b.type == "text")[:60],
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", default="AgentRouter")
    parser.add_argument("--model", default="claude-opus-5")
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--max-tokens", type=int, default=32)
    parser.add_argument("--label", default="")
    parser.add_argument("--persist", action="store_true")
    args = parser.parse_args()

    secret, base_url, provider_id, credential_id, _ = await credential(args.provider)

    from datetime import UTC, datetime

    window_start = datetime.now(UTC)
    before = await read_counter(base_url, secret)
    if before is None:
        raise SystemExit(f"{args.provider} does not expose {USAGE_PATH}; cannot measure")
    result = send(secret, base_url, args.model, args.prompt, args.max_tokens)
    # The counter is updated asynchronously; give it a moment to settle.
    await asyncio.sleep(4)
    after = await read_counter(base_url, secret)
    if after is None:
        raise SystemExit("counter unreadable after the request")

    contaminating = await concurrent_gateway_traffic(credential_id, window_start)
    raw_delta = round(after - before, 8)
    measured_cost = round(raw_delta / 100, 10)
    total_tokens = result["input_tokens"] + result["output_tokens"]

    report = {
        "label": args.label,
        "provider": args.provider,
        "requested_model": args.model,
        "response_model": result["model"],
        "text": result["text"],
        "input_tokens": result["input_tokens"],
        "output_tokens": result["output_tokens"],
        "cached_tokens": result["cached_tokens"],
        "total_tokens": total_tokens,
        "usage_before": before,
        "usage_after": after,
        "raw_delta": raw_delta,
        "measured_cost_usd_cent_scale": measured_cost,
        "blended_per_million": (
            round(measured_cost / total_tokens * 1_000_000, 6) if total_tokens else None
        ),
        "latency_ms": result["latency_ms"],
        "concurrent_gateway_attempts": contaminating,
        "trustworthy": contaminating == 0,
    }
    print(json.dumps(report, indent=2))

    if contaminating:
        print(
            f"\nREFUSING TO PERSIST: {contaminating} gateway attempt(s) used this "
            "credential during the window, so the billing delta is not attributable "
            "to this request alone.",
            file=sys.stderr,
        )
        raise SystemExit(2)

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
                raw_delta,
                measured_cost,
                result["input_tokens"],
                result["output_tokens"],
                result["cached_tokens"],
                f"Calibration sample: {args.label}",
            )
            print("persisted to cost_samples")
        finally:
            await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
