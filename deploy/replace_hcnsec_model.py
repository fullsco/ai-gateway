"""Replace the retired glm-5.2 mapping on hcnsec with what the provider actually serves.

api.hcnsec.cn no longer serves glm-5.2. Requesting it returns 503 model_not_found,
"No available channel for model glm-5.2 under group default (distributor)".

The replacement is not straightforward. Asking hcnsec for DeepSeek-V4-Pro does not
return DeepSeek: the provider advertises a list of branded names in /v1/models and
then serves unrelated backends. Measured directly against the provider:

    requested DeepSeek-V4-Pro   -> served nvidia/nemotron-3-ultra-550b-a55b
    requested Kimi-K2.6         -> served thinkingmachines/inkling
    requested Qwen3.8-27B       -> served meta/muse-glimmer-30b
    requested auto              -> served agnes-2.5-flash

So the canonical model is named for the model that actually answers,
nemotron-3-ultra, rather than for the name the provider happens to accept. The
upstream id stays DeepSeek-V4-Pro because that is the only string hcnsec routes:
nvidia/nemotron-3-ultra-550b-a55b, nemotron-3-ultra and nvidia/nemotron-3-ultra
are all rejected with model_not_found. That split is exactly what the canonical to
upstream mapping is for, and it means a caller asking for nemotron-3-ultra is
answered by nemotron-3-ultra.

The glm-5.2 mapping is disabled rather than deleted. request_attempts references it
with on delete set null, so deleting it would silently blank the provider on 32
historical attempts and lose the link to the flat-fee billing evidence recorded
against that route.

Routing is unchanged in shape: one provider, priority 100, allow_model_fallback
false, exactly as glm-5.2 had it.

The route is left unpriced. hcnsec bills a flat 640.94 counter units per request
regardless of token count, and the unit is ambiguous by a factor of five thousand,
so no honest per-token rate can be written down.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

SUPABASE = "https://bxpclohykdkbhnwizuop.supabase.co"
ADMIN = "http://127.0.0.1:8320/api/admin/v1"

PROVIDER = "hcnsec"
RETIRED_MODEL = "glm-5.2"
NEW_MODEL = "nemotron-3-ultra"
NEW_DISPLAY = "Nemotron 3 Ultra 550B"
UPSTREAM = "DeepSeek-V4-Pro"
SERVED = "nvidia/nemotron-3-ultra-550b-a55b"
CAPABILITIES = ["streaming", "tool_calling", "reasoning"]


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
            return error.code, {"raw": raw[:400].decode(errors="replace")}


def rows(payload):
    return payload.get("data", payload) if isinstance(payload, dict) else payload


def as_dict(value) -> dict:
    """The read API returns jsonb columns as strings, the write API demands objects."""
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return value or {}


def main() -> int:
    access = token()

    status, payload = call("GET", "/providers", access)
    provider = next(
        (row for row in rows(payload) if row.get("name") == PROVIDER),
        None,
    )
    if provider is None:
        print(f"provider {PROVIDER} not found")
        return 1
    provider_id = provider["id"]
    print(f"provider {PROVIDER} = {provider_id}")

    mappings = rows(call("GET", "/provider-models", access)[1])
    retired = next(
        (
            row
            for row in mappings
            if row.get("model_id") == RETIRED_MODEL and row.get("provider_id") == provider_id
        ),
        None,
    )
    if retired is None:
        print(f"no {RETIRED_MODEL} mapping on {PROVIDER} to retire")
    else:
        print(f"{RETIRED_MODEL} mapping = {retired['id']} (enabled={retired.get('enabled')})")

    existing_models = {row.get("id") for row in rows(call("GET", "/models", access)[1])}
    if NEW_MODEL in existing_models:
        print(f"canonical model {NEW_MODEL} already exists, left alone")
    else:
        status, created = call(
            "POST",
            "/models",
            access,
            {
                "id": NEW_MODEL,
                "display_name": NEW_DISPLAY,
                "capabilities": CAPABILITIES,
                "aliases": [],
                "enabled": True,
                # Context window is not published by the provider and was not
                # measured, so it is left unset rather than guessed.
            },
        )
        if status not in (200, 201):
            print(f"could not create canonical model: {status} {created}")
            return 1
        print(f"created canonical model {NEW_MODEL}")

    already = next(
        (
            row
            for row in mappings
            if row.get("model_id") == NEW_MODEL and row.get("provider_id") == provider_id
        ),
        None,
    )
    if already is not None:
        new_mapping_id = already["id"]
        print(f"mapping for {NEW_MODEL} already exists = {new_mapping_id}")
    else:
        status, created = call(
            "POST",
            "/provider-models",
            access,
            {
                "provider_id": provider_id,
                "model_id": NEW_MODEL,
                "upstream_model_id": UPSTREAM,
                "protocol": "openai_chat_completions",
                "capabilities": CAPABILITIES,
                "priority": 100,
                "weight": 1,
                "enabled": True,
                "max_concurrency": 8,
                "settings": {},
                # Deliberately unpriced. See the module docstring.
                "pricing": {},
            },
        )
        if status not in (200, 201):
            print(f"could not create mapping: {status} {json.dumps(created)[:300]}")
            return 1
        new_mapping_id = rows(created).get("id") if isinstance(created, dict) else None
        new_mapping_id = new_mapping_id or created.get("id")
        print(f"created mapping {NEW_MODEL} -> {PROVIDER} upstream={UPSTREAM} = {new_mapping_id}")

    status, routing = call(
        "PUT",
        f"/models/{NEW_MODEL}/routing",
        access,
        {
            "providers": [{"provider_id": provider_id, "priority": 100, "fallback": False}],
            "strategy": "priority",
            "health_aware": True,
            "quota_aware": True,
        },
    )
    if status not in (200, 201):
        print(f"could not set routing: {status} {json.dumps(routing)[:300]}")
        return 1
    print(f"routing set: {PROVIDER} priority 100, no model fallback")

    if retired is not None and retired.get("enabled"):
        body = {
            "provider_id": retired["provider_id"],
            "model_id": retired["model_id"],
            "upstream_model_id": retired["upstream_model_id"],
            "protocol": retired["protocol"],
            "capabilities": retired.get("capabilities") or [],
            "priority": retired.get("priority", 100),
            "weight": retired.get("weight", 1),
            "enabled": False,
            "max_concurrency": retired.get("max_concurrency", 8),
            "settings": as_dict(retired.get("settings")),
            "pricing": as_dict(retired.get("pricing")),
        }
        status, updated = call(
            "PUT", f"/provider-models/{retired['id']}", access, body
        )
        if status not in (200, 201):
            print(f"could not disable {RETIRED_MODEL}: {status} {json.dumps(updated)[:300]}")
            return 1
        print(f"disabled {RETIRED_MODEL} mapping, every other field preserved")
    elif retired is not None:
        print(f"{RETIRED_MODEL} mapping already disabled")

    # The canonical model has to be disabled too. Publishing refuses to strand an
    # enabled model with no usable route, which is the guard working correctly:
    # leaving glm-5.2 enabled with its only mapping disabled would mean a model in
    # the catalogue that can never be served. It is disabled rather than deleted so
    # the 72 historical requests naming it keep their meaning, and so it can be
    # re-enabled unchanged if the provider ever restores the channel.
    retired_model = next(
        (row for row in rows(call("GET", "/models", access)[1]) if row.get("id") == RETIRED_MODEL),
        None,
    )
    if retired_model is None:
        print(f"canonical model {RETIRED_MODEL} not present")
    elif not retired_model.get("enabled"):
        print(f"canonical model {RETIRED_MODEL} already disabled")
    else:
        status, updated = call(
            "PUT",
            f"/models/{RETIRED_MODEL}",
            access,
            {
                "id": RETIRED_MODEL,
                "display_name": retired_model.get("display_name") or RETIRED_MODEL,
                "capabilities": retired_model.get("capabilities") or [],
                "aliases": retired_model.get("aliases") or [],
                "enabled": False,
                "context_window": retired_model.get("context_window"),
            },
        )
        if status not in (200, 201):
            print(f"could not disable model {RETIRED_MODEL}: {status} {json.dumps(updated)[:300]}")
            return 1
        print(f"disabled canonical model {RETIRED_MODEL}, other fields preserved")

    print("\nnow publish the configuration and confirm the diff is only these changes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
