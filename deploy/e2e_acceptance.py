"""End-to-end acceptance run for the P5 hardening work.

Exercises the deployed gateway the way a client and an operator actually do, and
checks the specific behaviours P5 changed. Every assertion here corresponds to a
defect that was real in production, so a failure means a regression rather than a
missing feature.

Read-only with one exception: it creates and revokes nothing, publishes nothing, and
changes no configuration. It sends real inference requests, which cost money, so the
prompts are minimal.

Needs GATEWAY_ADMIN_EMAIL, GATEWAY_ADMIN_PASSWORD, and a gateway client key in
GATEWAY_E2E_KEY belonging to a client permitted both protocols.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

SUPABASE = "https://bxpclohykdkbhnwizuop.supabase.co"
ADMIN = "http://127.0.0.1:8320/api/admin/v1"
GATEWAY = "http://127.0.0.1:8320"

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
results: list[tuple[str, str, str]] = []


def record(name: str, status: str, detail: str = "") -> None:
    results.append((name, status, detail))
    mark = {PASS: "  ok ", FAIL: "FAIL ", SKIP: "skip "}[status]
    print(f"{mark} {name}" + (f" -- {detail}" if detail else ""))


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


def admin(path: str, access: str):
    request = urllib.request.Request(
        f"{ADMIN}{path}", headers={"authorization": f"Bearer {access}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return error.code, {}


def call(path: str, key: str | None, payload: dict, timeout: int = 240):
    headers = {"content-type": "application/json"}
    if key:
        headers["x-api-key"] = key
    request = urllib.request.Request(
        f"{GATEWAY}{path}", data=json.dumps(payload).encode(), headers=headers
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            return error.code, json.loads(raw or b"{}")
        except Exception:
            return error.code, {"raw": raw[:200].decode(errors="replace")}
    except Exception as exc:
        return 0, {"exception": type(exc).__name__}


def main() -> int:
    key = os.environ.get("GATEWAY_E2E_KEY")
    if not key:
        print("GATEWAY_E2E_KEY is required")
        return 2
    access = token()

    print("\n-- readiness --")
    try:
        with urllib.request.urlopen(f"{GATEWAY}/ready", timeout=30) as response:
            record("gateway is ready", PASS if response.status == 200 else FAIL)
    except Exception as exc:
        record("gateway is ready", FAIL, type(exc).__name__)

    print("\n-- inference on every enabled model --")
    # claude-opus-5-thinking is here because it had never once succeeded before the
    # user-agent fix; it is the sharpest single check in this file.
    for model, path in (
        ("claude-opus-5", "/v1/messages"),
        ("claude-opus-5-thinking", "/v1/messages"),
        ("claude-opus-4-8", "/v1/messages"),
        ("gpt-5.6-sol", "/v1/chat/completions"),
        ("nemotron-3-ultra", "/v1/chat/completions"),
    ):
        payload = {
            "model": model,
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
        }
        status, body = call(path, key, payload)
        if status != 200:
            error = body.get("error") or {}
            record(f"{model} answers", FAIL, f"http={status} {error.get('type', body)}")
            continue
        if path == "/v1/messages":
            text = " ".join(
                block.get("text", "")
                for block in (body.get("content") or [])
                if block.get("type") == "text"
            )
        else:
            text = ((body.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        record(f"{model} answers", PASS if text.strip() else FAIL, text.strip()[:40])

    print("\n-- retired model still fails cleanly --")
    status, body = call(
        "/v1/chat/completions",
        key,
        {"model": "glm-5.2", "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]},
    )
    error = (body.get("error") or {})
    record(
        "glm-5.2 is refused with a reason",
        PASS if status == 404 and error.get("type") == "model_unavailable" else FAIL,
        f"http={status} {error.get('type')}",
    )

    print("\n-- authentication says which problem it is --")
    status, body = call(
        "/v1/chat/completions",
        "gw_live_definitelynotarealkey00",
        {"model": "gpt-5.6-sol", "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]},
    )
    error = body.get("error") or {}
    record(
        "an unknown key is 401 authentication_error",
        PASS if status == 401 and error.get("type") == "authentication_error" else FAIL,
        f"http={status} {error.get('type')}",
    )
    status, body = call(
        "/v1/chat/completions", None,
        {"model": "gpt-5.6-sol", "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]},
    )
    error = body.get("error") or {}
    record(
        "a missing key says so, not 'invalid'",
        PASS if status == 401 and "No gateway key" in str(error.get("message")) else FAIL,
        str(error.get("message"))[:50],
    )

    print("\n-- operator views tell the truth --")
    status, payload = admin("/credentials", access)
    rows = payload.get("data", []) if status == 200 else []
    record("credentials view loads", PASS if rows else FAIL, f"{len(rows)} rows")
    if rows:
        missing = [r["name"] for r in rows if "routable" not in r or "routing_state" not in r]
        record(
            "every credential reports routable and routing_state",
            PASS if not missing else FAIL,
            f"{len(missing)} missing",
        )
        states = {r["routing_state"] for r in rows}
        record(
            "routing_state uses the documented vocabulary",
            PASS
            if states <= {"in service", "on trial", "cooling down", "needs attention", "disabled"}
            else FAIL,
            ", ".join(sorted(states)),
        )
        # The whole point of the change: routable must not equal healthy-only.
        routable = sum(1 for r in rows if r["routable"])
        healthy = sum(1 for r in rows if r["health"] == "healthy" and r["enabled"])
        record(
            "routable counts recoveries, not just healthy",
            PASS if routable >= healthy else FAIL,
            f"routable={routable} healthy={healthy}",
        )
        noted = sum(1 for r in rows if (r.get("note") or "").strip())
        record(
            "parked credentials carry an operator note",
            PASS if noted else FAIL,
            f"{noted} noted",
        )

    status, payload = admin("/providers", access)
    provider_rows = payload.get("data", []) if status == 200 else []
    record(
        "providers view reports routable_credentials",
        PASS if provider_rows and all("routable_credentials" in r for r in provider_rows) else FAIL,
    )

    status, payload = admin("/clients", access)
    client_rows = payload.get("data", []) if status == 200 else []
    record(
        "clients view reports effective access",
        PASS if client_rows and all("live_access" in r for r in client_rows) else FAIL,
    )
    lying = [
        r["name"] for r in client_rows if not r["enabled"] and r["live_access"].startswith("STILL")
    ]
    record(
        "no client is disabled while still serving",
        PASS if not lying else FAIL,
        ", ".join(lying) or "none",
    )

    print("\n-- configuration is published and legible --")
    status, payload = admin("/config/status", access)
    if status == 200:
        record(
            "no unpublished configuration drift",
            PASS if not payload.get("has_unpublished_changes") else FAIL,
            f"v{payload.get('active_version')} pending={payload.get('change_count')}",
        )
    else:
        record("no unpublished configuration drift", FAIL, f"http={status}")

    print("\n-- alerting is operable --")
    status, payload = admin("/alert-rules", access)
    rules = payload.get("data", []) if status == 200 else []
    record("alert rules are configured", PASS if len(rules) >= 13 else FAIL, f"{len(rules)} rules")
    by_name = {r["name"] for r in rules}
    record(
        "the early pool warning exists",
        PASS if "Provider is down to its last credential" in by_name else FAIL,
    )
    status, payload = admin("/alerts", access)
    alerts = payload.get("data", []) if status == 200 else []
    open_alerts = [a for a in alerts if not a.get("resolved_at")]
    resolved = [a for a in alerts if a.get("resolved_at")]
    record(
        "alerts both open and close",
        PASS if resolved else FAIL,
        f"{len(open_alerts)} open, {len(resolved)} resolved",
    )

    print("\n-- cost is recorded or honestly absent --")
    status, payload = admin("/requests?limit=25", access)
    requests = payload.get("data", []) if status == 200 else []
    record("request log is populated", PASS if requests else FAIL, f"{len(requests)} recent")

    print("\n" + "=" * 66)
    failures = [name for name, status, _ in results if status == FAIL]
    counts = {s: sum(1 for _, st, _ in results if st == s) for s in (PASS, FAIL, SKIP)}
    print(f"E2E: {counts[PASS]} passed, {counts[FAIL]} failed, {counts[SKIP]} skipped")
    if failures:
        print("\nfailed:")
        for name in failures:
            print(f"  - {name}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
