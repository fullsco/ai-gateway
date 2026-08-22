"""Measure GoRouter and TabiAi rates now that both are actually reachable.

Both were previously recorded as unmeasurable because every probe was refused. That
was a user-agent problem, not a network one: the gateway's OpenAI adapter sent no
user-agent and my own probes used urllib and curl, all of which Cloudflare refuses.
With ai-gateway/0.1 both providers answer, so the rates can be measured.

Method, matching the one already validated against AgentRouter: read the provider's
cumulative billing counter, send one request whose token counts are known from its
own usage block, read the counter again. The raw before and after values are kept so
the scale can be confirmed rather than assumed.

These two providers also report a per-request cost in the usage block, which is a
second, independent signal. If the counter delta and the reported cost agree, both
the scale and the meaning of that field are confirmed at once. If they disagree,
nothing is priced: the disagreement is recorded and the route stays unpriced, since
a rate that cannot be corroborated is a guess.

Samples deliberately vary the input and output mix so the two rates can be
separated. A control read with no traffic in between detects a counter moving for
somebody else's request, which would silently corrupt every figure.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from decimal import Decimal

import httpx

sys.path.insert(0, "/root/ai-gateway")

COUNTER_PATH = "/v1/dashboard/billing/usage"
UA = "ai-gateway/0.1"


async def load(provider_name: str):
    from gateway.config import Settings
    from gateway.configuration.postgres import PostgresSnapshotRepository, create_pool
    from gateway.configuration.runtime_builder import RuntimeBuilder

    settings = Settings(_env_file="/root/ai-gateway/.env")
    pool = await create_pool(settings.database_url)
    snapshot = await PostgresSnapshotRepository(pool).load_published()
    rows = await pool.fetch(
        """select c.id::text as id, c.name, c.provider_id::text as provider_id
           from public.provider_credentials c
           join public.providers p on p.id = c.provider_id
           where p.name = $1 and c.enabled
           order by coalesce(c.last_used_at, timestamptz 'epoch') asc, c.priority""",
        provider_name,
    )
    await pool.close()
    runtime = RuntimeBuilder(
        encryption_key=settings.credential_encryption_key,
        key_pepper=settings.key_pepper,
    ).build(snapshot.payload)
    provider = next(p for p in snapshot.payload["providers"] if p["name"] == provider_name)
    mapping = next(
        m
        for m in snapshot.payload["provider_models"]
        if m["provider_id"] == provider["id"] and m["canonical_model_id"] == "claude-opus-5"
    )
    if not rows:
        raise SystemExit(f"{provider_name} has no enabled credential")
    return settings, snapshot, runtime, provider, mapping, rows[0]


async def counter(http: httpx.AsyncClient, base_url: str, secret: str) -> Decimal | None:
    try:
        response = await http.get(
            f"{base_url.rstrip('/')}{COUNTER_PATH}",
            headers={"authorization": f"Bearer {secret}", "user-agent": UA},
        )
        if response.status_code != 200:
            return None
        return Decimal(str(response.json().get("total_usage")))
    except Exception:
        return None


SAMPLES = [
    ("short in, short out", "Reply with exactly: OK", 16),
    ("long in, short out", "Summarise in one word. " + ("context filler. " * 400), 16),
    ("short in, long out", "Count slowly from one to forty, one number per line.", 400),
]


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("provider")
    args = parser.parse_args()

    from gateway.protocols import ClientProtocol, NormalizedRequest
    from gateway.providers import Credential

    settings, snapshot, runtime, provider, mapping, row = await load(args.provider)
    adapter = runtime.provider_model_adapters[mapping["id"]]
    secret = runtime.credentials[row["id"]].secret
    base_url = provider["base_url"]
    print(f"{args.provider}: credential {row['name']}, base {base_url}\n")

    results = []
    async with httpx.AsyncClient(timeout=120) as http:
        control_a = await counter(http, base_url, secret)
        await asyncio.sleep(20)
        control_b = await counter(http, base_url, secret)
        if control_a is None:
            print("no billing counter available; cannot measure by delta")
        elif control_a != control_b:
            print(
                f"counter moved with no traffic of ours: {control_a} -> {control_b}. "
                "Another consumer is active, so every delta below would be corrupt."
            )
            return 1
        else:
            print(f"control: counter steady at {control_a} over 20s\n")

        for label, prompt, max_tokens in SAMPLES:
            request = NormalizedRequest(
                protocol=ClientProtocol.ANTHROPIC_MESSAGES,
                requested_model="claude-opus-5",
                stream=False,
                required_capabilities=frozenset(),
                payload={
                    "model": "claude-opus-5",
                    "max_tokens": max_tokens,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            upstream = adapter.create_request(
                request, Credential(id=row["id"], secret=secret)
            )
            before = await counter(http, base_url, secret)
            try:
                response = await http.post(
                    upstream.url, headers=upstream.headers, json=upstream.json_body
                )
            except Exception as exc:
                print(f"  {label:22} {type(exc).__name__}: {str(exc)[:60]}")
                continue
            if response.status_code != 200:
                print(f"  {label:22} http={response.status_code} {response.text[:70]}")
                continue
            body = response.json()
            usage = body.get("usage") or {}
            await asyncio.sleep(6)
            after = await counter(http, base_url, secret)
            delta = (after - before) if (before is not None and after is not None) else None
            results.append(
                {
                    "label": label,
                    "input_tokens": usage.get("input_tokens"),
                    "output_tokens": usage.get("output_tokens"),
                    "reported_cost": usage.get("cost"),
                    "counter_before": str(before),
                    "counter_after": str(after),
                    "counter_delta": str(delta),
                }
            )
            tokens = f"in={usage.get('input_tokens')} out={usage.get('output_tokens')}"
            print(
                f"  {label:22} {tokens:22} "
                f"reported_cost={usage.get('cost')} counter_delta={delta}"
            )
            await asyncio.sleep(4)

    print("\n--- corroboration ---")
    priceable = True
    for entry in results:
        delta = entry["counter_delta"]
        reported = entry["reported_cost"]
        if delta in (None, "None") or reported is None:
            print(f"  {entry['label']:22} cannot corroborate (missing a signal)")
            priceable = False
            continue
        as_usd = Decimal(delta) / Decimal(100)
        ratio = (as_usd / Decimal(str(reported))) if Decimal(str(reported)) else None
        print(
            f"  {entry['label']:22} counter/100=${as_usd} reported=${reported} "
            f"ratio={ratio.quantize(Decimal('0.001')) if ratio else 'n/a'}"
        )
    print(json.dumps(results, indent=2))
    print(
        "\nA ratio near 1.000 across samples confirms the counter is cent-scale and that "
        "the reported cost is USD. Anything else means the route stays unpriced."
    )
    return 0 if priceable else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
