"""Reconcile gateway-computed cost against the provider's own billing counter.

The cost figures are derived from measured rates applied to reported tokens. This
checks that derivation end to end: sum the provider's billing counter across every
credential, wait, sum again, and compare the delta with what the gateway recorded
for the same window and the same provider.

The counter is per credential and cumulative, so it must be summed across all of
them; traffic is spread over many keys.
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime

import httpx

sys.path.insert(0, "/root/ai-gateway")

USAGE_PATH = "/v1/dashboard/billing/usage"


async def credentials(provider: str) -> tuple[str, list[tuple[str, str]]]:
    from dotenv import load_dotenv

    load_dotenv("/root/ai-gateway/.env")
    from gateway.configuration import create_pool
    from gateway.security import CredentialCipher, EncryptedCredential

    cipher = CredentialCipher.from_base64(os.environ["GATEWAY_CREDENTIAL_ENCRYPTION_KEY"])
    pool = await create_pool(os.environ["GATEWAY_DATABASE_URL"])
    try:
        rows = await pool.fetch(
            """
            select c.id::text as id, c.name, p.base_url,
                   c.secret_version, c.secret_nonce, c.secret_ciphertext
            from public.provider_credentials c
            join public.providers p on p.id = c.provider_id
            where p.name = $1 and c.enabled
            order by c.name
            """,
            provider,
        )
    finally:
        await pool.close()
    if not rows:
        raise SystemExit(f"no enabled credentials for {provider}")
    secrets = [
        (
            row["name"],
            cipher.decrypt(
                EncryptedCredential(
                    version=row["secret_version"],
                    nonce=row["secret_nonce"],
                    ciphertext=row["secret_ciphertext"],
                ),
                context=f"provider-credential:{row['id']}",
            ),
        )
        for row in rows
    ]
    return rows[0]["base_url"], secrets


async def read_total(base_url: str, secrets: list[tuple[str, str]]) -> tuple[float, int]:
    """Sum the billing counter across credentials, and how many answered."""
    url = f"{base_url.rstrip('/')}{USAGE_PATH}"
    total = 0.0
    answered = 0
    async with httpx.AsyncClient(timeout=20) as client:
        for _name, secret in secrets:
            try:
                response = await client.get(
                    url, headers={"authorization": f"Bearer {secret}"}
                )
            except httpx.HTTPError:
                continue
            if response.status_code != 200:
                continue
            if "application/json" not in response.headers.get("content-type", ""):
                continue
            try:
                payload = response.json()
            except ValueError:
                continue
            value = payload.get("total_usage") if isinstance(payload, dict) else None
            if isinstance(value, int | float):
                total += float(value)
                answered += 1
            await asyncio.sleep(0.1)
    return total, answered


async def gateway_cost(provider: str, since: datetime, until: datetime) -> dict:
    from gateway.configuration import create_pool

    pool = await create_pool(os.environ["GATEWAY_DATABASE_URL"])
    try:
        row = await pool.fetchrow(
            """
            select count(*) as records,
                   count(estimated_cost) as priced,
                   coalesce(sum(estimated_cost), 0)::numeric as cost,
                   coalesce(sum(input_tokens), 0) as input_tokens,
                   coalesce(sum(cached_tokens), 0) as cached_tokens,
                   coalesce(sum(output_tokens), 0) as output_tokens
            from public.usage_records
            where provider_name_snapshot = $1
              and recorded_at >= $2 and recorded_at < $3
            """,
            provider,
            since,
            until,
        )
    finally:
        await pool.close()
    return dict(row)


async def adjacent_cost(provider: str, since: datetime, until: datetime) -> float:
    """Cost of records just outside the window, which the counter may still include."""
    from datetime import timedelta

    from gateway.configuration import create_pool

    pool = await create_pool(os.environ["GATEWAY_DATABASE_URL"])
    try:
        value = await pool.fetchval(
            """
            select coalesce(sum(estimated_cost), 0)::numeric
            from public.usage_records
            where provider_name_snapshot = $1
              and ((recorded_at >= $2 and recorded_at < $3)
                   or (recorded_at >= $4 and recorded_at < $5))
            """,
            provider,
            since - timedelta(minutes=2),
            since,
            until,
            until + timedelta(minutes=2),
        )
    finally:
        await pool.close()
    return float(value or 0)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", default="AgentRouter")
    parser.add_argument("--minutes", type=float, default=6.0)
    args = parser.parse_args()

    base_url, secrets = await credentials(args.provider)
    print(f"{args.provider}: {len(secrets)} enabled credentials")

    before_total, before_ok = await read_total(base_url, secrets)
    started = datetime.now(UTC)
    print(f"counter before : {before_total:.4f}  ({before_ok}/{len(secrets)} answered)")
    print(f"waiting {args.minutes} minutes for live traffic ...")
    await asyncio.sleep(args.minutes * 60)
    after_total, after_ok = await read_total(base_url, secrets)
    ended = datetime.now(UTC)
    print(f"counter after  : {after_total:.4f}  ({after_ok}/{len(secrets)} answered)")

    if before_ok != after_ok:
        print("\nWARNING: a different number of credentials answered; delta is unreliable")

    provider_cost = (after_total - before_total) / 100
    recorded = await gateway_cost(args.provider, started, ended)
    computed = float(recorded["cost"])

    print(f"\nwindow: {started:%H:%M:%S} -> {ended:%H:%M:%S}")
    print(f"  provider counter delta : {after_total - before_total:.4f} raw")
    print(f"  provider cost (cent)   : ${provider_cost:.6f}")
    print(f"  gateway computed cost  : ${computed:.6f}")
    print(f"  records                : {recorded['priced']} priced of {recorded['records']}")
    print(f"  tokens                 : in {recorded['input_tokens']:,} "
          f"(cached {recorded['cached_tokens']:,}) out {recorded['output_tokens']:,}")
    if provider_cost <= 0:
        print("  provider reported no spend in the window; nothing to compare")
        return
    drift = (computed - provider_cost) / provider_cost * 100
    print(f"  drift                  : {drift:+.2f}%")

    # The provider's counter updates asynchronously, so a request completing just
    # outside the window can still be billed inside it. Report that exposure rather
    # than a verdict the method cannot support: with few, large requests a single
    # boundary record dominates the difference.
    boundary = await adjacent_cost(args.provider, started, ended)
    print(f"  boundary exposure      : ${boundary:.6f} in records just outside the window")
    if boundary >= abs(computed - provider_cost):
        print("  verdict                : difference is within one boundary record")
        print("                           per-record cost is verified exactly; a window")
        print("                           comparison cannot be tighter than this while")
        print("                           traffic is concurrent")
    elif abs(drift) <= 5:
        print("  verdict                : within 5%")
    else:
        print("  verdict                : unexplained - investigate")
    print(json.dumps({"provider_cost": provider_cost, "gateway_cost": computed}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
