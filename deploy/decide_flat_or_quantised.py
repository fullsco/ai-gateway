"""Decide whether these providers bill a flat fee or a quantised per-token rate.

Every earlier sample was small: 138 to 8,783 input tokens. Across that range the
counter moved by an identical amount every time, which reads as a flat per-request
fee. But a coarse minimum charge looks exactly the same from inside a narrow range.
If the counter is quantised rather than flat, a much larger request moves it by more,
and the rate can be derived and priced per token in the normal way, with no change to
the pricing model.

So this sends a small request and a large one and compares. The distinction matters
before writing any code:

  flat       delta identical at 8k and at 200k tokens  -> needs a per-request fee shape
  quantised  delta grows with size                     -> ordinary per-token pricing

A control read with no traffic of ours brackets the run, because a counter moving for
somebody else's request would corrupt both readings and could manufacture either
answer.
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


async def load(provider_name: str, model: str):
    from gateway.config import Settings
    from gateway.configuration.postgres import PostgresSnapshotRepository, create_pool
    from gateway.configuration.runtime_builder import RuntimeBuilder

    settings = Settings(_env_file="/root/ai-gateway/.env")
    pool = await create_pool(settings.database_url)
    snapshot = await PostgresSnapshotRepository(pool).load_published()
    rows = await pool.fetch(
        """select c.id::text as id, c.name from public.provider_credentials c
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
        if m["provider_id"] == provider["id"] and m["canonical_model_id"] == model
    )
    if not rows:
        raise SystemExit(f"{provider_name} has no enabled credential")
    return runtime, provider, mapping, rows[0]


async def counter(http: httpx.AsyncClient, base_url: str, secret: str) -> Decimal | None:
    for _ in range(3):
        try:
            response = await http.get(
                f"{base_url.rstrip('/')}{COUNTER_PATH}",
                headers={"authorization": f"Bearer {secret}", "user-agent": UA},
            )
            if response.status_code == 200:
                return Decimal(str(response.json().get("total_usage")))
        except Exception:
            pass
        await asyncio.sleep(3)
    return None


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("provider")
    parser.add_argument("--model", default="claude-opus-5")
    parser.add_argument("--large-words", type=int, default=60000)
    args = parser.parse_args()

    from gateway.protocols import ClientProtocol, NormalizedRequest
    from gateway.providers import Credential

    runtime, provider, mapping, row = await load(args.provider, args.model)
    adapter = runtime.provider_model_adapters[mapping["id"]]
    secret = runtime.credentials[row["id"]].secret
    base_url = provider["base_url"]
    anthropic = mapping["protocol"] == ClientProtocol.ANTHROPIC_MESSAGES.value

    # One word is roughly one token, so this reaches a size where a minimum charge
    # cannot possibly dominate.
    filler = " ".join(f"w{n}" for n in range(args.large_words))
    cases = [
        ("small", "Reply with exactly: OK", 16),
        ("large", f"Reply with exactly: OK. Ignore this: {filler}", 16),
    ]

    print(f"{args.provider} / {args.model} via {base_url}, credential {row['name']}\n")
    observations = []
    async with httpx.AsyncClient(timeout=300) as http:
        start = await counter(http, base_url, secret)
        await asyncio.sleep(15)
        control = await counter(http, base_url, secret)
        if start is None or control is None:
            print("counter unreadable; cannot decide")
            return 1
        if start != control:
            print(f"counter moved with no traffic of ours ({start} -> {control}); aborting")
            return 1
        print(f"control: steady at {start}\n")

        for label, prompt, max_tokens in cases:
            request = NormalizedRequest(
                protocol=ClientProtocol(mapping["protocol"]),
                requested_model=args.model,
                stream=False,
                required_capabilities=frozenset(),
                payload={
                    "model": args.model,
                    "max_tokens": max_tokens,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            upstream = adapter.create_request(request, Credential(id=row["id"], secret=secret))
            before = await counter(http, base_url, secret)
            try:
                response = await http.post(
                    upstream.url, headers=upstream.headers, json=upstream.json_body
                )
            except Exception as exc:
                print(f"  {label:6} {type(exc).__name__}: {str(exc)[:60]}")
                continue
            if response.status_code != 200:
                print(f"  {label:6} http={response.status_code} {response.text[:80]}")
                continue
            body = response.json()
            usage = body.get("usage") or {}
            if anthropic:
                tokens_in = usage.get("input_tokens")
                tokens_out = usage.get("output_tokens")
            else:
                tokens_in = usage.get("prompt_tokens")
                tokens_out = usage.get("completion_tokens")
            await asyncio.sleep(8)
            after = await counter(http, base_url, secret)
            delta = (after - before) if (before is not None and after is not None) else None
            observations.append((label, tokens_in, tokens_out, delta, usage.get("cost")))
            print(
                f"  {label:6} in={tokens_in} out={tokens_out} delta={delta} "
                f"reported_cost={usage.get('cost')}"
            )
            await asyncio.sleep(5)

    print("\n--- verdict ---")
    if len(observations) < 2 or any(o[3] is None for o in observations):
        print("could not close both deltas; inconclusive")
        return 1
    small, large = observations[0], observations[1]
    token_ratio = Decimal(large[1]) / Decimal(small[1]) if small[1] else None
    if large[3] == small[3]:
        print(
            f"FLAT: {small[1]} and {large[1]} input tokens both moved the counter by "
            f"{small[3]}, a {token_ratio:.0f}x token increase for no change in charge. "
            "A per-token rate cannot describe this."
        )
    else:
        per_token = (large[3] - small[3]) / (Decimal(large[1]) - Decimal(small[1]))
        print(
            f"SCALES: delta {small[3]} -> {large[3]} over {small[1]} -> {large[1]} input "
            f"tokens, i.e. {per_token} counter units per input token, "
            f"{per_token * 1_000_000} per million. Ordinary per-token pricing applies."
        )
    print(json.dumps([{
        "case": o[0], "input_tokens": o[1], "output_tokens": o[2],
        "counter_delta": str(o[3]), "reported_cost": o[4],
    } for o in observations], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
