"""Poll per-credential spend from providers that actually expose it.

Investigation of the configured providers found exactly one trustworthy signal:
one-api/new-api style relays expose ``GET /v1/dashboard/billing/usage`` which
returns ``total_usage`` for the *calling credential*. It is cumulative spend, not
a remaining balance.

Two consequences shape this module:

* ``/v1/dashboard/billing/subscription`` is NOT used. It returns a hardcoded
  ``hard_limit_usd`` of 100000000 identically across different credentials and
  providers, so treating it as a quota ceiling would be a silent correctness bug.
* Because no ceiling is discoverable, ``total_usage`` alone cannot produce a
  headroom figure. It is recorded as ``quota_used`` with provenance
  ``upstream_usage``; real headroom only exists once an operator also sets
  ``quota_limit``. Until then the router treats the quota dimension as unknown
  rather than inventing a denominator.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from gateway.logging import log_event
from gateway.security.credentials import CredentialCipher, EncryptedCredential

logger = logging.getLogger("gateway.health.usage")

USAGE_PATH = "/v1/dashboard/billing/usage"
_TIMEOUT = httpx.Timeout(10.0)


@dataclass(frozen=True)
class UsageObservation:
    credential_id: str
    provider_id: str
    total_usage: float
    unit: str


async def poll_credential_usage(
    pool: Any,
    encryption_key: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> list[UsageObservation]:
    """Record cumulative spend for credentials whose provider exposes it.

    Never raises: a provider that does not implement the endpoint, is down, or
    returns something unexpected simply yields no observation for that credential.
    """
    rows = await pool.fetch(
        """
        select c.id::text as id, c.provider_id::text as provider_id,
               c.secret_version, c.secret_nonce, c.secret_ciphertext,
               p.base_url, p.name as provider_name
        from public.provider_credentials c
        join public.providers p on p.id = c.provider_id
        where c.enabled and p.enabled
        """
    )
    if not rows:
        return []

    cipher = CredentialCipher.from_base64(encryption_key)
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=_TIMEOUT)
    observations: list[UsageObservation] = []
    try:
        for row in rows:
            try:
                secret = cipher.decrypt(
                    EncryptedCredential(
                        version=row["secret_version"],
                        nonce=row["secret_nonce"],
                        ciphertext=row["secret_ciphertext"],
                    ),
                    context=f"provider-credential:{row['id']}",
                )
            except Exception:
                continue

            usage = await _fetch_usage(http, str(row["base_url"]), secret)
            if usage is None:
                continue
            observations.append(
                UsageObservation(
                    credential_id=row["id"],
                    provider_id=row["provider_id"],
                    total_usage=usage,
                    unit="provider_native",
                )
            )
            await pool.execute(
                """
                update public.provider_credentials
                set quota_used = $2,
                    quota_source = 'upstream_usage',
                    quota_observed_at = now(),
                    quota_note = $3,
                    updated_at = now()
                where id = $1
                """,
                row["id"],
                usage,
                (
                    "Cumulative spend reported by the provider "
                    f"({USAGE_PATH}). Units are provider-native; set quota_limit "
                    "in the same units to enable quota-aware routing."
                ),
            )
            await asyncio.sleep(0.2)  # be gentle with upstream abuse protection
    finally:
        if owns_client:
            await http.aclose()

    log_event(
        logger,
        logging.INFO,
        "credential_usage_polled",
        observed=len(observations),
        candidates=len(rows),
    )
    return observations


async def _fetch_usage(
    client: httpx.AsyncClient, base_url: str, secret: str
) -> float | None:
    url = base_url.rstrip("/") + USAGE_PATH
    try:
        response = await client.get(
            url, headers={"Authorization": f"Bearer {secret}"}, timeout=_TIMEOUT
        )
    except Exception:
        return None
    if response.status_code != 200:
        return None
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
    if content_type != "application/json":
        # Some domains answer 200 with an HTML parking page; do not trust it.
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("total_usage")
    if not isinstance(value, (int, float)):
        return None
    return float(value)


async def usage_poll_loop(
    interval_seconds: float,
    pool_getter,
    encryption_key: str | None,
) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        pool = pool_getter()
        if pool is None or not encryption_key:
            continue
        try:
            await poll_credential_usage(pool, encryption_key)
        except Exception as exc:  # pragma: no cover - defensive
            log_event(
                logger,
                logging.WARNING,
                "credential_usage_poll_failed",
                error_type=type(exc).__name__,
            )


__all__ = ["UsageObservation", "poll_credential_usage", "usage_poll_loop", "USAGE_PATH"]
