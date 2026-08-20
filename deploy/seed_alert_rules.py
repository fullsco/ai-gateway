"""Seed the starter alert rule set through the admin API.

Every rule is written in the operator's language: what happened, why it matters
and what to do. Thresholds are deliberately conservative where no measured
baseline exists yet, and the ones that need calibration say so in their text.
"""

import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("GATEWAY_ADMIN_URL", "http://127.0.0.1:8320/api/admin/v1")
SUPABASE = "https://bxpclohykdkbhnwizuop.supabase.co"

RULES = [
    {
        "name": "Provider is failing most requests",
        "severity": "critical",
        "condition_kind": "provider_failure_rate",
        "condition": {"window_minutes": 15, "at_least": 0.5, "min_requests": 10},
        "cooldown_seconds": 900,
        "description": (
            "More than half of the attempts sent to this provider failed in the last 15 minutes."
        ),
        "impact": (
            "Requests still succeed by failing over, but every failure adds latency and, on a "
            "metered provider, cost. If the alternatives also degrade, requests start failing "
            "outright."
        ),
        "recommended_action": (
            "Check the provider's status, then Health for which credentials are affected. If it "
            "stays bad, lower the provider's priority so it is used only as a fallback."
        ),
    },
    {
        "name": "Provider cannot be reached at all",
        "severity": "critical",
        "condition_kind": "provider_unreachable",
        "condition": {"window_minutes": 15, "at_least": 3, "min_requests": 3},
        "cooldown_seconds": 900,
        "description": (
            "Every attempt to this provider in the last 15 minutes failed to connect, timed out, "
            "or was blocked before reaching the model."
        ),
        "impact": (
            "This provider is contributing no capacity. Any model that depends on it alone is "
            "down for clients."
        ),
        "recommended_action": (
            "Confirm the provider's base URL is reachable. A block at the provider's edge is not "
            "a credential problem, so rotating keys will not help."
        ),
    },
    {
        "name": "A credential keeps being rejected",
        "severity": "warning",
        "condition_kind": "credential_auth_failures",
        "condition": {"window_minutes": 30, "at_least": 5},
        "cooldown_seconds": 1800,
        "description": (
            "This credential was rejected by the provider at least 5 times in the last 30 "
            "minutes."
        ),
        "impact": (
            "The credential is repeatedly parked and retried, which wastes an attempt on every "
            "request that selects it."
        ),
        "recommended_action": (
            "Rotate the key, or disable the credential if it has been revoked. Check first that "
            "the provider is not returning a challenge page, which is not a key problem."
        ),
    },
    {
        "name": "Credential is nearly out of quota",
        "severity": "warning",
        "condition_kind": "credential_quota_low",
        "condition": {"at_least": 0.9},
        "cooldown_seconds": 3600,
        "description": "This credential has used at least 90% of its configured quota.",
        "impact": (
            "Routing already prefers credentials with more headroom. When the quota is gone the "
            "credential stops accepting traffic entirely."
        ),
        "recommended_action": (
            "Raise the quota, add another credential to the provider, or let it roll over if the "
            "limit is periodic."
        ),
    },
    {
        "name": "Credential balance is nearly gone",
        "severity": "warning",
        "condition_kind": "credential_balance_low",
        "condition": {"at_least": 1.0},
        "cooldown_seconds": 3600,
        "description": (
            "The last observed balance for this credential is at or below 1.00 in its own "
            "currency."
        ),
        "impact": (
            "A provider that holds a preauthorisation will start refusing requests before running "
            "them once the balance falls under the amount it reserves."
        ),
        "recommended_action": (
            "Top the credential up. Balance is only as fresh as its last observation, shown next "
            "to it in Credentials."
        ),
    },
    {
        "name": "Provider has no usable credentials left",
        "severity": "critical",
        "condition_kind": "credential_pool_exhausted",
        "condition": {"at_least": 0},
        "cooldown_seconds": 900,
        "description": "Every enabled credential on this provider is currently unusable.",
        "impact": (
            "The provider can serve nothing. Models that list it as their only provider are down "
            "for clients."
        ),
        "recommended_action": (
            "Open Credentials for this provider and look at why each one is out: rejected, rate "
            "limited, out of quota, or cooling down after failures."
        ),
    },
    {
        "name": "A model has nowhere to run",
        "severity": "critical",
        "condition_kind": "model_no_eligible_route",
        "condition": {"window_minutes": 15, "at_least": 3},
        "cooldown_seconds": 900,
        "description": (
            "Requests for this model were refused at least 3 times in the last 15 minutes because "
            "no route was eligible."
        ),
        "impact": (
            "Clients are getting errors for this model. The model is configured, so this is "
            "capacity or health rather than a missing mapping."
        ),
        "recommended_action": (
            "Open a failed request's trace to see which candidates were excluded and why, then "
            "fix that cause or add another provider for the model."
        ),
    },
    {
        "name": "A model is failing for clients",
        "severity": "warning",
        "condition_kind": "request_failure_rate",
        "condition": {"window_minutes": 30, "at_least": 0.25, "min_requests": 20},
        "cooldown_seconds": 1800,
        "description": (
            "At least a quarter of client requests for this model failed in the last 30 minutes."
        ),
        "impact": (
            "This is what the client actually experiences, after all failover has been attempted."
        ),
        "recommended_action": (
            "Compare with the provider alerts. If no single provider is at fault, the model may "
            "have too few providers to absorb one going bad."
        ),
    },
    {
        "name": "Spending is unusually high",
        "severity": "warning",
        "condition_kind": "cost_spike",
        "condition": {"window_minutes": 60, "at_least": 10.0},
        "cooldown_seconds": 3600,
        "description": (
            "Measured spend in the last hour reached 10.00 USD. This threshold is a starting "
            "point and should be set from your own measured baseline."
        ),
        "impact": (
            "An hour well above normal usually means a client is looping, or traffic has moved to "
            "a more expensive provider."
        ),
        "recommended_action": (
            "Check Analytics for which model and provider the spend came from, and whether "
            "retries are inflating it. Adjust this threshold once a normal day has been measured."
        ),
    },
    {
        "name": "Traffic is not being priced",
        "severity": "warning",
        "condition_kind": "unpriced_traffic",
        "condition": {"window_minutes": 60, "at_least": 0.5, "min_requests": 20},
        "cooldown_seconds": 3600,
        "description": (
            "At least half the usage recorded in the last hour has no cost attached, because the "
            "route serving it has no pricing configured."
        ),
        "impact": (
            "Unpriced traffic is invisible in cost reporting, and a blocking budget cannot charge "
            "against it, so a spend cap can be reached without being enforced."
        ),
        "recommended_action": (
            "Add pricing for the provider and model shown in Analytics, or measure it if the "
            "provider's rates are not published."
        ),
    },
]


def token() -> str:
    with open("/root/ai-gateway/dashboard/.env.local") as handle:
        key = next(
            line.split("=", 1)[1].strip().strip('"')
            for line in handle
            if line.startswith("NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY=")
        )
    request = urllib.request.Request(
        f"{SUPABASE}/auth/v1/token?grant_type=password",
        data=json.dumps(
            {
                "email": os.environ["GATEWAY_ADMIN_EMAIL"],
                "password": os.environ["GATEWAY_ADMIN_PASSWORD"],
            }
        ).encode(),
        headers={"apikey": key, "content-type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())["access_token"]


def main() -> None:
    access = token()
    existing = urllib.request.Request(
        f"{BASE}/alert-rules", headers={"authorization": f"Bearer {access}"}
    )
    with urllib.request.urlopen(existing, timeout=60) as response:
        present = {row["name"] for row in json.loads(response.read())["data"]}

    created = skipped = 0
    for rule in RULES:
        if rule["name"] in present:
            print(f"  exists, left alone : {rule['name']}")
            skipped += 1
            continue
        request = urllib.request.Request(
            f"{BASE}/alert-rules",
            data=json.dumps(rule).encode(),
            method="POST",
            headers={"authorization": f"Bearer {access}", "content-type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                print(f"  created {response.status}        : {rule['name']}")
                created += 1
        except urllib.error.HTTPError as exc:
            print(f"  FAILED {exc.code}         : {rule['name']}")
            print(f"      {exc.read().decode()[:300]}")
            sys.exit(1)
    print(f"\ncreated {created}, already present {skipped}")


if __name__ == "__main__":
    main()
