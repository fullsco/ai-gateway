"""Verify provider-scoped failover against the real published configuration.

The unit test proves the executor logic on a synthetic runtime. This proves the
same thing on the configuration the gateway is actually serving: it loads the
published snapshot, builds the real runtime, and drives the real routing engine.

A live HTTP probe is not usable here for two reasons. Client keys are compiled
into the published snapshot rather than read live, so probing would require
publishing a throwaway config version to production. And both providers for this
model are Cloudflare-blocked from this host, so a live call could not distinguish
"fallback reached and failed" from "fallback never attempted" without reading the
trace anyway. Driving the routing engine over the published snapshot answers the
routing question directly and mutates nothing.

For each model it reports the provider chosen per attempt under two policies:

  credential-only  what the executor did before the fix, retiring only the
                   failed credential
  provider-scoped  what it does now, retiring the whole provider when the error
                   is scoped to the provider

The failure being reproduced: GoRouter has more credentials than the attempt
budget, so under credential-only every attempt stayed on GoRouter and the
configured TabiAi fallback was unreachable.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, datetime

from gateway.config import Settings
from gateway.configuration.postgres import PostgresSnapshotRepository, create_pool
from gateway.configuration.runtime_builder import RuntimeBuilder
from gateway.protocols import ClientProtocol
from gateway.protocols.models import NormalizedRequest
from gateway.routing.engine import NoRouteAvailable, RoutingTrace

MODELS = ("claude-opus-5-thinking", "claude-opus-5", "gpt-5.6-sol", "glm-5.2")
MAX_ATTEMPTS = 3
# Mirrors gateway.routing.engine.ROUTABLE_HEALTH.
ROUTABLE = ("healthy", "degraded")


def _is_routable(state, now) -> bool:
    """Mirror the engine's credential health gate, including recovery trials.

    An unhealthy credential whose cooldown has elapsed is routable again: health
    is only restored by observing a success, so without a trial it would be
    stranded permanently.
    """
    if not state.enabled:
        return False
    if state.health.value in ROUTABLE:
        return True
    return state.cooldown_until is not None and state.cooldown_until <= now


def _normalized(runtime, model: str) -> NormalizedRequest | None:
    """Build a request over whichever protocol actually exposes this model.

    Models are mapped per protocol. Asking for an OpenAI-only model over
    anthropic_messages yields no candidates, which would otherwise be reported as
    "no fallback configured" when the real answer is "not exposed here".
    """
    for protocol in (ClientProtocol.ANTHROPIC_MESSAGES, ClientProtocol.OPENAI_CHAT_COMPLETIONS):
        request = NormalizedRequest(
            protocol=protocol,
            requested_model=model,
            stream=False,
            required_capabilities=frozenset(),
            payload={"model": model, "max_tokens": 16, "messages": []},
        )
        try:
            if runtime.model_registry.eligible_provider_models(request):
                return request
        except LookupError:
            continue
    return None


def _walk(runtime, normalized, *, provider_scoped: bool) -> list[str]:
    """Return the provider chosen at each attempt under the given retirement policy."""
    excluded_credentials: frozenset[str] = frozenset()
    excluded_routes: frozenset[str] = frozenset()
    chosen: list[str] = []

    for _ in range(MAX_ATTEMPTS):
        try:
            route = runtime.routing_engine.select(
                normalized,
                list(runtime.provider_states),
                list(runtime.credential_states),
                excluded_credential_ids=excluded_credentials,
                excluded_provider_model_ids=excluded_routes,
                trace=RoutingTrace(attempt_number=len(chosen) + 1),
                diagnostics={},
            )
        except (LookupError, NoRouteAvailable):
            break

        provider_id = route.provider_model.provider_id
        chosen.append(provider_id)

        if not route.provider_model.allow_model_fallback:
            excluded_routes = excluded_routes | {
                candidate.id
                for candidate in runtime.model_registry.eligible_provider_models(normalized)
                if candidate.id != route.provider_model.id
            }

        excluded_credentials = excluded_credentials | {route.credential.credential_id}

        if provider_scoped:
            siblings = {
                model.id
                for model in runtime.model_registry.list_provider_models()
                if model.provider_id == provider_id
            }
            another_provider_is_reachable = any(
                candidate.id not in excluded_routes and candidate.id not in siblings
                for candidate in runtime.model_registry.eligible_provider_models(normalized)
            )
            if another_provider_is_reachable:
                excluded_routes = excluded_routes | siblings

    return chosen


async def main() -> int:
    settings = Settings(_env_file="/root/ai-gateway/.env")
    if not settings.database_url:
        print("GATEWAY_DATABASE_URL is not configured")
        return 1

    pool = await create_pool(settings.database_url)
    try:
        snapshot = await PostgresSnapshotRepository(pool).load_published()
        if snapshot is None:
            print("no published configuration snapshot")
            return 1
        runtime = RuntimeBuilder(
            encryption_key=settings.credential_encryption_key,
            key_pepper=settings.key_pepper,
        ).build(snapshot.payload)
    finally:
        await pool.close()

    names: dict[str, str] = {}
    for provider in snapshot.payload.get("providers", []):
        names[provider["id"]] = provider.get("name", provider["id"])

    def label(provider_id: str) -> str:
        return names.get(provider_id, provider_id)

    print(f"published snapshot: v{snapshot.version} schema={snapshot.schema_version}\n")

    now = datetime.now(UTC)
    failures = 0
    for model in MODELS:
        normalized = _normalized(runtime, model)
        if normalized is None:
            print(f"{model}\n  no provider mapping on any supported protocol, skipped\n")
            continue
        before = _walk(runtime, normalized, provider_scoped=False)
        after = _walk(runtime, normalized, provider_scoped=True)

        routes = runtime.model_registry.eligible_provider_models(normalized)
        healthy_by_provider = {
            route.provider_id: sum(
                1
                for state in runtime.credential_states
                if state.provider_id == route.provider_id and _is_routable(state, now)
            )
            for route in routes
        }
        total_by_provider = {
            route.provider_id: sum(
                1 for state in runtime.credential_states if state.provider_id == route.provider_id
            )
            for route in routes
        }
        primary_route = next(
            (route for route in routes if before and route.provider_id == before[0]), None
        )

        print(f"{model}  [{normalized.protocol.value}]")
        primary = label(before[0]) if before else "none, all routes ineligible"
        print(f"  primary provider          : {primary}")
        print("  credentials routable/total (routable includes recovery trials):")
        for route in sorted(routes, key=lambda r: r.priority):
            ratio = (
                f"{healthy_by_provider[route.provider_id]:>2}"
                f"/{total_by_provider[route.provider_id]:<2}"
            )
            print(
                f"      {label(route.provider_id):12} {ratio} "
                f"priority={route.priority:<4} allow_model_fallback={route.allow_model_fallback}"
            )
        print(f"  credential-only (was)     : {[label(p) for p in before]}")
        print(f"  provider-scoped (now)     : {[label(p) for p in after]}")

        distinct_before = len(dict.fromkeys(before))
        distinct_after = len(dict.fromkeys(after))
        configured = len({route.provider_id for route in routes})

        if configured < 2:
            print("  verdict                   : single provider, nothing to fail over to\n")
            continue
        if distinct_after > distinct_before:
            print("  verdict                   : FIXED, fallback provider now reached\n")
        elif distinct_after >= 2:
            print("  verdict                   : already crossed providers\n")
        elif primary_route is not None and not primary_route.allow_model_fallback:
            # Not a routing defect. The mapping forbids serving this canonical model
            # from another provider, so the only failover available is a sibling
            # credential on the primary provider.
            spare = healthy_by_provider.get(primary_route.provider_id, 0) - 1
            print(
                "  verdict                   : by policy, allow_model_fallback=False forbids "
                f"another provider; {spare} spare routable credential(s) on the primary\n"
            )
            if spare < 1:
                failures += 1
        else:
            failures += 1
            print(
                f"  verdict                   : STILL BROKEN, {configured} providers configured "
                "but only 1 attempted\n"
            )

    return 1 if failures else 0


if __name__ == "__main__":
    os.environ.pop("GATEWAY_DATABASE_URL", None)
    sys.exit(asyncio.run(main()))
