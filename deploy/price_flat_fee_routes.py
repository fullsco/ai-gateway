"""Price the flat-fee routes from their published rate cards.

These three routes were unpriced because a flat per-request fee could not be
expressed. Two things changed. The pricing model now has a per_request shape, and the
providers turned out to publish a rate card at /api/pricing that corroborates the
measurements exactly.

The convention, confirmed on all three providers, is the one-api family's: quota_type
1 means a flat model_price per request in USD, with model_ratio 0; quota_type 0 means
a per-token ratio instead. The billing counter is in cents, which the measured deltas
confirm independently:

    TabiAi   claude-opus-5  listed 0.8  measured counter delta 80   -> 80/100 = 0.80
    GoRouter claude-opus-5  listed 0.3  measured counter delta 30   -> 30/100 = 0.30

The same convention validates the existing AgentRouter figures without touching them.
Its card lists claude-opus-5 at model_ratio 1 with completion_ratio 5, and at the
family's base of $2 per million that is $2 in and $10 out, exactly what was measured
here months earlier. gpt-5.6-sol lists ratio 2, so $4 and $20, also exactly as
measured. Two independent derivations agreeing is the strongest evidence this project
has for any rate.

Confidence is set from the evidence actually held for each route: high where a listed
price and a measured counter delta agree, medium where only the published card was
read. hcnsec is left alone: its /api/pricing returns an empty catalogue, so
nemotron-3-ultra stays unpriced.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

SUPABASE = "https://bxpclohykdkbhnwizuop.supabase.co"
ADMIN = "http://127.0.0.1:8320/api/admin/v1"

# (provider, canonical model) -> (fee, confidence, evidence)
PRICES = {
    ("TabiAi", "claude-opus-5"): (
        "0.80",
        "high",
        "listed model_price 0.8 at quota_type 1, and a measured counter delta of 80 "
        "cents for both a 7,185 and a 246,190 input-token request",
    ),
    ("TabiAi", "claude-opus-5-thinking"): (
        "0.80",
        "medium",
        "listed model_price 0.8 at quota_type 1; the flat-fee behaviour and cent scale "
        "were measured on this provider's claude-opus-5 route",
    ),
    ("GoRouter", "claude-opus-5"): (
        "0.30",
        "high",
        "listed model_price 0.3 at quota_type 1, and a measured counter delta of 30 "
        "cents per request across three requests of differing size",
    ),
    ("GoRouter", "claude-opus-5-thinking"): (
        "0.30",
        "medium",
        "listed model_price 0.3 at quota_type 1; the flat-fee behaviour and cent scale "
        "were measured on this provider's claude-opus-5 route",
    ),
    ("GoRouter", "claude-opus-4-8"): (
        "0.20",
        "medium",
        "listed model_price 0.2 at quota_type 1; the flat-fee behaviour and cent scale "
        "were measured on this provider's claude-opus-5 route",
    ),
}


def _publishable_key() -> str:
    with open("/root/ai-gateway/dashboard/.env.local") as handle:
        return next(
            line.split("=", 1)[1].strip().strip('"')
            for line in handle
            if line.startswith("NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY=")
        )


def token() -> str:
    request = urllib.request.Request(
        f"{SUPABASE}/auth/v1/token?grant_type=password",
        data=json.dumps(
            {
                "email": os.environ["GATEWAY_ADMIN_EMAIL"],
                "password": os.environ["GATEWAY_ADMIN_PASSWORD"],
            }
        ).encode(),
        headers={"apikey": _publishable_key(), "content-type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())["access_token"]


def call(method: str, path: str, access: str, body: dict | None = None):
    request = urllib.request.Request(
        f"{ADMIN}{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"authorization": f"Bearer {access}", "content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            raw = response.read()
            return response.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            return error.code, json.loads(raw)
        except Exception:
            return error.code, {"raw": raw[:300].decode(errors="replace")}


def as_dict(value) -> dict:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return value or {}


def main() -> int:
    access = token()
    status, payload = call("GET", "/provider-models", access)
    if status != 200:
        print(f"could not read mappings: {status}")
        return 1
    mappings = payload["data"]

    applied = skipped = 0
    for (provider, model), (fee, confidence, evidence) in PRICES.items():
        row = next(
            (
                m
                for m in mappings
                if m.get("provider_name") == provider and m.get("model_id") == model
            ),
            None,
        )
        if row is None:
            print(f"  no mapping: {provider} / {model}")
            continue
        existing = as_dict(row.get("pricing"))
        if existing.get("per_request") == fee:
            print(f"  already priced: {provider} / {model} at {fee}")
            skipped += 1
            continue
        if existing and "per_request" not in existing:
            # Never quietly replace a token rate with a flat fee: that would be a
            # different billing model, and the disagreement needs a human.
            print(f"  REFUSING {provider} / {model}: already has token pricing {existing}")
            continue
        body = {
            "provider_id": row["provider_id"],
            "model_id": row["model_id"],
            "upstream_model_id": row["upstream_model_id"],
            "protocol": row["protocol"],
            "capabilities": row.get("capabilities") or [],
            "priority": row.get("priority", 100),
            "weight": row.get("weight", 1),
            "enabled": row.get("enabled", True),
            "max_concurrency": row.get("max_concurrency", 8),
            "settings": as_dict(row.get("settings")),
            "pricing": {
                "per_request": fee,
                "currency": "USD",
                "pricing_basis": "listed",
                "confidence": confidence,
            },
        }
        code, out = call("PUT", f"/provider-models/{row['id']}", access, body)
        if code not in (200, 201):
            print(f"  FAILED {provider} / {model}: {code} {json.dumps(out)[:200]}")
            continue
        print(f"  priced {provider} / {model} at ${fee} per request ({confidence}) -- {evidence}")
        applied += 1

    print(f"\napplied {applied}, already correct {skipped}")

    status, payload = call("GET", "/provider-models", access)
    print("\npricing coverage now:")
    ordered = sorted(
        payload["data"],
        key=lambda r: (str(r.get("model_id")), str(r.get("provider_name"))),
    )
    for m in ordered:
        pricing = as_dict(m.get("pricing"))
        if "per_request" in pricing:
            shape = f"${pricing['per_request']}/request"
        elif "input_per_million" in pricing:
            shape = f"${pricing['input_per_million']}/${pricing['output_per_million']} per 1M"
        elif "blended_per_million" in pricing:
            shape = f"${pricing['blended_per_million']} per 1M blended"
        else:
            shape = "UNPRICED"
        print(f"  {str(m.get('model_id')):24} {str(m.get('provider_name')):12} {shape}")
    print("\nPublish to make these effective.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
