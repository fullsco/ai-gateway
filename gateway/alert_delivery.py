import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import time
from urllib.parse import urlsplit

import httpx

from gateway.alert_delivery_store import AlertDeliveryStore
from gateway.logging import log_event

logger = logging.getLogger("gateway.alert_delivery")


def validate_destination(value):
    try:
        if len(value) > 2048 or any(ord(c) <= 32 or ord(c) >= 127 for c in value):
            raise ValueError
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.fragment or "#" in value
                or "\\" in value or parsed.port not in (None, 443)):
            raise ValueError
        host = parsed.hostname
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if (len(host) > 253 or "." not in host
                    or host in ("localhost", "metadata.google.internal")
                    or host.endswith((".localhost", ".local"))
                    or any(not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?",
                                            label) for label in host.split("."))):
                raise ValueError(
                    "Alert delivery requires a trusted HTTPS destination"
                ) from None
        else:
            if not address.is_global:
                raise ValueError
    except (ValueError, TypeError):
        raise ValueError(
            "Alert delivery requires a trusted HTTPS destination on port 443"
        ) from None
    return value


def signed_payload(row, secret):
    payload = {
        "version": 1, "event_id": str(row["id"]), "alert_id": str(row["alert_id"]),
        "sequence": row["sequence"], "status": row["status"],
        "previous_status": row["previous_status"], "severity": row["severity"],
        "occurred_at": row["occurred_at"].isoformat(),
    }
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    timestamp = str(int(time.time()))
    signature = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    return body, {
        "Content-Type": "application/json", "X-Gateway-Event-ID": str(row["id"]),
        "X-Gateway-Timestamp": timestamp, "X-Gateway-Signature": "sha256=" + signature.hexdigest(),
    }


async def deliver_one(store, client, settings):
    row = await store.claim(
        settings.alert_delivery_destination, max_attempts=settings.alert_delivery_max_attempts,
        lease_seconds=settings.alert_delivery_timeout_seconds + 30,
        max_age_seconds=settings.alert_delivery_max_age_seconds,
    )
    if row is None:
        return False
    body, headers = signed_payload(row, settings.alert_delivery_secret.get_secret_value())
    status, error, outcome = None, None, "pending"
    try:
        async with asyncio.timeout(settings.alert_delivery_timeout_seconds):
            async with client.stream(
                "POST", settings.alert_delivery_url.get_secret_value(),
                content=body, headers=headers,
            ) as response:
                status = response.status_code
                if 200 <= status < 300:
                    outcome = "delivered"
                elif status in (408, 429) or status >= 500:
                    error = "http_transient"
                else:
                    outcome, error = "dead", "http_permanent"
    except (httpx.HTTPError, TimeoutError):
        error = "transport_error"
    if outcome == "pending" and row["attempts"] >= settings.alert_delivery_max_attempts:
        outcome = "dead"
    delay = min(3600, 5 * 2 ** min(row["attempts"] - 1, 10)) if outcome == "pending" else 0
    await store.finish(row["id"], row["lease_token"], outcome=outcome,
                       delay_seconds=delay, error=error, status_code=status)
    return True


async def alert_delivery_loop(pool, settings):
    if not settings.alert_delivery_enabled:
        return
    store = AlertDeliveryStore(pool)
    async with httpx.AsyncClient(
        trust_env=False, follow_redirects=False, verify=True,
        timeout=settings.alert_delivery_timeout_seconds,
        limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
    ) as client:
        while True:
            try:
                async with asyncio.timeout(settings.alert_delivery_timeout_seconds + 20):
                    await deliver_one(store, client, settings)
            except Exception:
                log_event(logger, logging.WARNING, "alert_delivery_iteration_failed")
            await asyncio.sleep(settings.alert_delivery_poll_seconds)


async def start_alert_delivery(resources, pool, settings):
    roles = {"combined", "maintenance"}
    if not settings.alert_delivery_enabled or settings.service_role not in roles:
        return
    if pool is None:
        raise RuntimeError("Alert delivery requires a database")
    await AlertDeliveryStore(pool).require_schema()
    resources.task(alert_delivery_loop(pool, settings))
