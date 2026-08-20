"""Configuration hygiene, applied through the admin API so every change is audited.

Three things make the current configuration hard to read, and each is fixed by the
smallest safe change:

* Six clients exist, three of which have no keys and have never served a request.
  They are indistinguishable in the dashboard because they share names with the
  live ones. They are disabled and named for what they are, not deleted, so the
  change is reversible and no key or history is touched.

* Some client requests name a provider's own internal model id, because a client
  echoed back the model name a response carried. Those are the same model, so they
  are registered as aliases. Names that refer to a genuinely different model are
  deliberately left failing - aliasing them would silently serve something the
  caller did not ask for.

Nothing is deleted, no secret is read or written, and every field not being
changed is read first and passed back unchanged.
"""

import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("GATEWAY_ADMIN_URL", "http://127.0.0.1:8320/api/admin/v1")
SUPABASE = "https://bxpclohykdkbhnwizuop.supabase.co"

# Upstream ids observed in real responses from AgentRouter, which load balances
# across several backends for the same model. A client that echoes one of these
# back is asking for the same model.
ALIASES: dict[str, list[str]] = {
    "claude-opus-5": [
        "anthropic/claude-opus-5-ps-aws-dst",
        "anthropic/claude-opus-5-aws",
        "anthropic.claude-opus-5",
    ],
    "claude-opus-4-8": [
        "MaaS_Cl_Opus_4.8_20260528_cache",
    ],
}

# Requested names that refer to a different model than anything configured. These
# must keep failing until a provider actually serves them; an alias would answer
# with the wrong model at the wrong price.
LEFT_FAILING = {
    "claude-sonnet-5": "a Sonnet class model, and no provider is configured for it",
    "kimi-k3": "not an Anthropic or OpenAI model configured on any provider",
}


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


ACCESS = ""


def api(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    request = urllib.request.Request(
        f"{BASE}/{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"authorization": f"Bearer {ACCESS}", "content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
            return response.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        print(f"    {method} {path} -> {exc.code}: {exc.read()[:300].decode()}")
        raise


def tidy_models(apply: bool) -> None:
    print("\nModel aliases")
    _, payload = api("GET", "models")
    models = {row["id"]: row for row in payload["data"]}
    for model_id, aliases in ALIASES.items():
        model = models.get(model_id)
        if model is None:
            print(f"  skip   {model_id}: not configured")
            continue
        existing = set(model.get("aliases") or [])
        missing = [alias for alias in aliases if alias not in existing]
        if not missing:
            print(f"  ok     {model_id}: aliases already present")
            continue
        print(f"  add    {model_id}: {', '.join(missing)}")
        if not apply:
            continue
        # Every other field is read back unchanged; only aliases are added to.
        body = {
            "id": model["id"],
            "display_name": model["display_name"],
            "capabilities": list(model.get("capabilities") or []),
            "aliases": sorted(existing | set(aliases)),
            "enabled": model["enabled"],
            "context_window": model.get("context_window"),
        }
        status, _ = api("PUT", f"models/{model_id}", body)
        print(f"         -> {status}")
    for name, reason in LEFT_FAILING.items():
        print(f"  leave  {name}: still unroutable on purpose - {reason}")


async def clients_with_history() -> set[str]:
    """Clients that have ever served a request, read directly and read-only.

    A client with traffic in its past is never retired here, even if it currently
    has no usable key: the history is what makes it worth keeping legible.
    """
    from dotenv import load_dotenv

    load_dotenv("/root/ai-gateway/.env")
    from gateway.configuration import create_pool

    pool = await create_pool(os.environ["GATEWAY_DATABASE_URL"])
    try:
        rows = await pool.fetch(
            "select distinct client_id::text as client_id from public.request_logs"
        )
    finally:
        await pool.close()
    return {row["client_id"] for row in rows}


def tidy_clients(apply: bool) -> None:
    import asyncio

    print("\nUnused duplicate clients")
    _, clients = api("GET", "clients")
    served = asyncio.run(clients_with_history())

    for client in clients["data"]:
        client_id = str(client["id"])
        has_keys = int(client.get("active_keys") or 0) > 0
        has_history = client_id in served
        if has_keys or has_history or not client["enabled"]:
            reason = (
                "has an active key" if has_keys
                else "has request history" if has_history
                else "already disabled"
            )
            print(f"  keep   {client['name']:<18} {client_id[:8]}  ({reason})")
            continue
        renamed = client["name"]
        if "unused" not in renamed.lower():
            renamed = f"{client['name']} (unused duplicate)"
        print(f"  retire {client['name']:<18} {client_id[:8]}  -> disabled, renamed")
        if not apply:
            continue
        body = {
            "name": renamed,
            "allowed_protocols": list(client.get("allowed_protocols") or []),
            "allowed_models": list(client.get("allowed_models") or []),
            "enabled": False,
            "requests_per_minute": client.get("requests_per_minute"),
            "tokens_per_minute": client.get("tokens_per_minute"),
            "spending_limit": client.get("spending_limit"),
        }
        status, _ = api("PUT", f"clients/{client_id}", body)
        print(f"         -> {status}")


def main() -> None:
    global ACCESS
    apply = "--apply" in sys.argv
    ACCESS = token()
    print("APPLYING CHANGES" if apply else "DRY RUN - pass --apply to make changes")
    tidy_models(apply)
    tidy_clients(apply)
    print("\nNothing was deleted. No credential or routing relationship was touched.")


if __name__ == "__main__":
    main()
