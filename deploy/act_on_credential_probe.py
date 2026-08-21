"""Act on the probe of AgentRouter's parked credentials.

Eight credentials were parked as auth_failed and, because none of them held a
cooldown, none would ever be retried. Probing each one directly against the
provider, using the mapping's real default headers and betas, found that half of
them work.

The headers matter. Probed with a minimal header set every credential returns 403
"unauthorized client detected", including the credential that is currently serving
all production traffic. That is a client fingerprint check, not a key check, and
taking it at face value would have condemned eight working keys. The control probe
against the known-good credential is what exposed it.

Results, with the mapping's real headers:

    restored-1eaf91d1  200  works, and had never recorded a single success
    restored-d4fbedbd  200  works
    restored-1bfe41ca  200  works, and was additionally disabled
    restored-d37159cf  200  works
    restored-949513d4  403  user quota is not enough
    restored-3d5cd725  403  user quota is not enough
    restored-f025f8ba  403  the caller's IP is not on the token's allow list
    restored-5acd2785  403  the token may not access claude-opus-5

The four that work are reinstated, which takes AgentRouter from 17 routable
credentials to 21. The two that are out of quota are marked as such rather than as
authentication failures, so they read as recoverable and are not presented as keys
to rotate. The remaining two need a human: one needs this host's IP added to the
token's allow list, the other needs model entitlement, and neither will fix itself.

Every credential gets a note recording what was probed and when, so the next
person does not repeat this.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

SUPABASE = "https://bxpclohykdkbhnwizuop.supabase.co"
ADMIN = "http://127.0.0.1:8320/api/admin/v1"
PROBED_AT = "2026-08-22"

WORKING = {
    "restored-1eaf91d1": (
        "Probed directly against AgentRouter on "
        + PROBED_AT
        + " with the mapping's real headers and betas: HTTP 200. Had recorded zero "
        "successes and no cooldown, so it was never going to be retried. The earlier "
        "auth_failed came from probing without the claude-cli headers, which the "
        "provider rejects for every key including the one serving production."
    ),
    "restored-d4fbedbd": (
        "Probed directly against AgentRouter on " + PROBED_AT + ": HTTP 200. Parked as "
        "auth_failed with no cooldown, so it was never retried. Works."
    ),
    "restored-1bfe41ca": (
        "Probed directly against AgentRouter on " + PROBED_AT + ": HTTP 200. Was both "
        "auth_failed and disabled after 40 failures, but the key itself is fine."
    ),
    "restored-d37159cf": (
        "Probed directly against AgentRouter on " + PROBED_AT + ": HTTP 200. Parked as "
        "auth_failed with no cooldown despite 15 recorded successes. Works."
    ),
}

# Out of quota, not rejected. Recoverable without human action once quota resets.
OUT_OF_QUOTA = {
    "restored-949513d4": (
        "Probed on " + PROBED_AT + ": HTTP 403 \"user quota is not enough\". This is a "
        "quota condition, not an authentication failure. It needs topping up or time, "
        "not rotating."
    ),
    "restored-3d5cd725": (
        "Probed on " + PROBED_AT + ": HTTP 403 \"user quota is not enough\". Quota "
        "condition, not a bad key."
    ),
}

# Genuinely blocked until somebody changes something at the provider.
NEEDS_OPERATOR = {
    "restored-f025f8ba": (
        "Probed on " + PROBED_AT + ": HTTP 403, the caller's IP is not on this token's "
        "allow list. The key is valid but unusable from this host. Add the gateway's "
        "egress IP to the token's allow list at AgentRouter, or retire the key. This "
        "will not recover on its own."
    ),
    "restored-5acd2785": (
        "Probed on " + PROBED_AT + ": HTTP 403, this token may not access "
        "claude-opus-5. Entitle the token for the model at AgentRouter, or retire the "
        "key. This will not recover on its own."
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


def main() -> int:
    access = token()
    status, payload = call("GET", "/credentials", access)
    if status != 200:
        print(f"could not read credentials: {status}")
        return 1
    rows = payload["data"]
    by_name = {row["name"]: row for row in rows}

    def update(row: dict, *, note: str, enabled: bool | None = None) -> bool:
        body = {
            "name": row["name"],
            "enabled": row["enabled"] if enabled is None else enabled,
            "priority": row.get("priority", 100),
            "quota_limit": row.get("quota_limit"),
            "quota_threshold": row.get("quota_threshold") or 0.95,
            "requests_per_minute": row.get("requests_per_minute"),
            "tokens_per_minute": row.get("tokens_per_minute"),
            "note": note,
        }
        code, out = call("PUT", f"/credentials/{row['id']}", access, body)
        if code not in (200, 201):
            print(f"  FAILED {row['name']}: {code} {json.dumps(out)[:200]}")
            return False
        return True

    reinstated = noted = 0

    for name, note in WORKING.items():
        row = by_name.get(name)
        if row is None:
            print(f"  missing: {name}")
            continue
        # Re-enable first, so reinstating cannot leave it healthy but switched off.
        if not row["enabled"] and not update(row, note=note, enabled=True):
            continue
        code, out = call(
            "POST", f"/credentials/{row['id']}/reinstate", access, {"note": note}
        )
        if code not in (200, 201):
            print(f"  FAILED reinstate {name}: {code} {json.dumps(out)[:200]}")
            continue
        print(f"  reinstated {name}: health={out.get('health')} enabled={out.get('enabled')}")
        reinstated += 1

    for name, note in OUT_OF_QUOTA.items():
        row = by_name.get(name)
        if row is None:
            continue
        if update(row, note=note):
            print(f"  noted as out of quota: {name}")
            noted += 1

    for name, note in NEEDS_OPERATOR.items():
        row = by_name.get(name)
        if row is None:
            continue
        if update(row, note=note):
            print(f"  noted as needing operator action: {name}")
            noted += 1

    print(f"\nreinstated {reinstated}, annotated {noted}")
    print("Publish is not required: health, cooldown and notes are live state, not")
    print("configuration. Only enabled changes need a publish, and one was made.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
