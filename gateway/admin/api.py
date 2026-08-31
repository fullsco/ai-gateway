import json
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from gateway.admin.auth import AdminClaims, SupabaseJWTVerifier
from gateway.routing.engine import ROUTABLE_CREDENTIAL_SQL

router = APIRouter(prefix="/api/admin/v1", tags=["admin"])


async def _authorize(request: Request) -> AdminClaims | JSONResponse:
    verifier: SupabaseJWTVerifier | None = getattr(request.app.state, "admin_verifier", None)
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if verifier is None or scheme.lower() != "bearer" or not token:
        return JSONResponse({"error": "admin_authentication_required"}, status_code=401)
    try:
        return await verifier.verify(token)
    except PermissionError:
        return JSONResponse({"error": "administrator_role_required"}, status_code=403)
    except ValueError:
        return JSONResponse({"error": "invalid_administrator_session"}, status_code=401)


def _pool(request: Request) -> Any | None:
    pool = getattr(request.app.state, "db_pool", None)
    return getattr(request.state, "control_plane_connection", pool)


@router.get("/overview")
async def overview(request: Request) -> JSONResponse:
    claims = await _authorize(request)
    if isinstance(claims, JSONResponse):
        return claims
    pool = _pool(request)
    if pool is None:
        return JSONResponse({"error": "control_plane_unavailable"}, status_code=503)
    row = await pool.fetchrow(
        """
        select
          count(*) filter (where started_at >= current_date) as requests_today,
          count(*) filter (where started_at >= date_trunc('month', now())) as requests_month,
          count(*) filter (where status = 'succeeded' and started_at >= current_date) as successful,
          count(*) filter (where status = 'failed' and started_at >= current_date) as failed,
          avg(latency_ms) filter (
            where started_at >= current_date and ended_at is not null
          ) as avg_latency_ms,
          avg((fallback_count > 0)::int) filter (where started_at >= current_date) as fallback_rate
        from public.request_logs
        """
    )
    provider_row = await pool.fetchrow(
        """
        select count(*) filter (where enabled) as active_providers,
               count(*) filter (where enabled and health = 'healthy') as healthy_providers
        from public.providers
        """
    )
    key_row = await pool.fetchrow(
        """
        select count(*) filter (where enabled) as active_keys,
               count(*) filter (where cooldown_until > now()) as keys_in_cooldown
        from public.provider_credentials
        """
    )
    cost_row = await pool.fetchrow(
        """
        select sum(input_tokens) as input_tokens,
               sum(output_tokens) as output_tokens,
               count(*) filter (where estimated_cost is not null) as priced_records,
               count(*) as usage_records
        from public.usage_records
        where recorded_at >= date_trunc('month', now())
        """
    )
    cost_rows = await pool.fetch(
        """
        select currency, sum(estimated_cost) as estimated_cost
        from public.usage_records
        where recorded_at >= date_trunc('month', now())
          and estimated_cost is not null and currency is not null
        group by currency order by currency
        """
    )
    return JSONResponse(
        jsonable_encoder(
            {
                **dict(row),
                **dict(provider_row),
                **dict(key_row),
                **dict(cost_row),
                "costs_by_currency": [dict(item) for item in cost_rows],
                "runtime_ready": request.app.state.ready,
                "config_version": getattr(
                    getattr(request.app.state, "runtime_manager", None), "version", None
                ),
            }
        ),
    )


@router.get("/providers")
async def providers(request: Request) -> JSONResponse:
    return await _list_query(
        request,
        """
         select p.id, p.name, p.provider_type, p.protocol, p.base_url, p.enabled,
               p.priority, p.capabilities, p.health, p.timeout_seconds, p.settings,
               p.created_at, p.updated_at,
               count(c.id) as credential_count,
               count(c.id) filter (where c.enabled and c.health = 'healthy') as healthy_credentials,
               -- What the router can actually use, which includes an unhealthy
               -- credential whose cooldown has elapsed. Counting only healthy ones
               -- made a pool of 17 usable credentials read as 1.
               count(c.id) filter (where c.enabled and (""" + ROUTABLE_CREDENTIAL_SQL + """))
                 as routable_credentials
        from public.providers p
        left join public.provider_credentials c on c.provider_id = p.id
        group by p.id
        order by p.priority, p.name
        """,
    )


@router.get("/models")
async def models(request: Request) -> JSONResponse:
    return await _list_query(
        request,
        """
         select m.id, m.display_name, m.enabled, m.capabilities, m.context_window,
                 m.created_at, m.updated_at,
                coalesce(
                  array_agg(distinct a.alias order by a.alias)
                    filter (where a.alias is not null), '{}'
                ) as aliases,
                count(distinct pm.id) as provider_route_count,
                coalesce(
                  array_agg(distinct p.name order by p.name)
                    filter (where pm.enabled and p.enabled), '{}'
                ) as available_through,
                coalesce(
                  array_agg(distinct pm.protocol order by pm.protocol)
                    filter (where pm.enabled and p.enabled), '{}'
                ) as protocols
         from public.models m
         left join public.model_aliases a on a.model_id = m.id
         left join public.provider_models pm on pm.model_id = m.id and pm.enabled
         left join public.providers p on p.id = pm.provider_id
         group by m.id
        order by m.id
        """,
    )


@router.get("/credentials")
async def credentials(request: Request) -> JSONResponse:
    return await _list_query(
        request,
        """
        select c.id, c.provider_id, p.name as provider_name, c.name, c.masked_hint,
               c.enabled, c.priority, c.health, c.quota_limit, c.quota_used,
               c.quota_threshold, c.requests_per_minute, c.tokens_per_minute,
               c.cooldown_until, c.last_used_at, c.last_success_at, c.last_failure_at,
               c.success_count, c.failure_count, c.created_at, c.updated_at,
               -- Provenance, so a quota figure is never read as a measurement it
               -- is not. Without a limit there is no denominator and headroom is
               -- unknowable, however precise quota_used looks.
               c.quota_source, c.quota_observed_at, c.quota_note,
               -- Whether the router will actually try this credential, evaluated with
               -- the router's own predicate so the two cannot disagree. Health alone
               -- understated the pool badly: one provider read as 1 healthy out of 25
               -- while the router could use 17, because an unhealthy credential whose
               -- cooldown has elapsed earns a recovery attempt.
               (c.enabled and (""" + ROUTABLE_CREDENTIAL_SQL + """)) as routable,
               -- What the operator should do about it, which health cannot express.
               -- The important distinction is between a credential that will come
               -- back on its own and one that never will.
               case
                 when not c.enabled then 'disabled'
                 when c.cooldown_until is not null and c.cooldown_until > now()
                   then 'cooling down'
                 when c.health in ('healthy','degraded') then 'in service'
                 when c.cooldown_until is not null then 'on trial'
                 else 'needs attention'
               end as routing_state,
               case
                 when c.quota_limit is not null and c.quota_limit <> 0
                      and c.quota_used is not null then 'known'
                 when c.quota_source = 'upstream_usage' and c.quota_used is not null
                      then 'estimated'
                 else 'unknown'
               end as quota_confidence,
               c.balance_amount, c.balance_currency, c.balance_observed_at,
               c.balance_source, c.note
        from public.provider_credentials c
        join public.providers p on p.id = c.provider_id
        order by p.priority, c.priority, c.name
        """,
    )


@router.get("/clients")
async def clients(request: Request) -> JSONResponse:
    return await _list_query(
        request,
        """
        select c.id, c.name, c.enabled, c.allowed_protocols, c.allowed_models,
               c.requests_per_minute, c.tokens_per_minute, c.spending_limit,
               count(k.id) filter (where k.enabled and k.revoked_at is null) as active_keys,
               -- Access is enforced from the published snapshot, not from this table,
               -- so a client edited here is unchanged in practice until a publish.
               -- Disabling a client and seeing "disabled" in this list, while its keys
               -- keep working, is the most dangerous version of that gap, so the
               -- effective state is reported next to the configured one.
               case
                 when pub.published_enabled is null and c.enabled
                   then 'not serving yet, publish to activate'
                 when pub.published_enabled is null then 'not serving'
                 when pub.published_enabled and c.enabled then 'serving'
                 when pub.published_enabled and not c.enabled
                   then 'STILL SERVING until you publish'
                 when not pub.published_enabled and c.enabled
                   then 'not serving until you publish'
                 else 'not serving'
               end as live_access,
               c.created_at, c.updated_at
        from public.gateway_clients c
        left join public.gateway_client_keys k on k.client_id = c.id
        left join lateral (
          select (element->>'enabled')::boolean as published_enabled
          from public.config_versions v,
               lateral jsonb_array_elements(v.payload->'clients') as element
          where v.status = 'published' and element->>'id' = c.id::text
          limit 1
        ) pub on true
        group by c.id, pub.published_enabled
        order by c.name
        """,
    )


@router.get("/routes")
async def routes(request: Request) -> JSONResponse:
    return await _list_query(
        request,
        """
        select r.id, r.model_id, r.provider_model_id, pm.provider_id, p.name as provider_name,
               pm.upstream_model_id, pm.protocol, r.priority, r.enabled,
               r.allow_model_fallback, r.policy_id, r.pool_id, rp.name as policy_name, r.created_at
        from public.model_routes r
        join public.provider_models pm on pm.id = r.provider_model_id
        join public.providers p on p.id = pm.provider_id
        left join public.routing_policies rp on rp.id = r.policy_id
        order by r.model_id, r.priority
        """,
    )


@router.get("/providers/{provider_id}/workspace")
async def provider_workspace(provider_id: str, request: Request) -> JSONResponse:
    return await _list_query(
        request,
        """
        select p.id,p.name,p.enabled,p.health,p.base_url,p.priority,
               count(distinct c.id) as credential_count,
               count(distinct c.id) filter (where c.enabled and c.health='healthy')
                 as healthy_credentials,
               count(distinct c.id)
                 filter (where c.enabled and (""" + ROUTABLE_CREDENTIAL_SQL + """))
                 as routable_credentials,
               count(distinct c.id) filter (where c.cooldown_until > now())
                 as cooling_credentials,
               count(distinct pm.id) as mapping_count,
               count(distinct pm.model_id) as model_count,
               coalesce(array_agg(distinct pm.protocol order by pm.protocol)
                 filter (where pm.protocol is not null), '{}') as protocols
        from public.providers p
        left join public.provider_credentials c on c.provider_id=p.id
        left join public.provider_models pm on pm.provider_id=p.id and pm.enabled
        where p.id=$1
        group by p.id
        """,
        provider_id,
    )


@router.get("/provider-models")
async def provider_models(request: Request) -> JSONResponse:
    return await _list_query(
        request,
        """
        select pm.id, pm.provider_id, p.name as provider_name, pm.model_id,
                pm.upstream_model_id, pm.protocol, pm.serves_protocols,
                 pm.capabilities, pm.priority,
                 pm.weight, pm.max_concurrency, pm.enabled, pm.pricing,
                 pm.settings,
                pm.created_at, pm.updated_at
        from public.provider_models pm
        join public.providers p on p.id = pm.provider_id
        order by pm.model_id, pm.priority, p.name
        """,
    )


@router.get("/routing-policies")
async def routing_policies(request: Request) -> JSONResponse:
    response = await _list_query(
        request,
        """select id,name,enabled,policy,created_at,updated_at
           from public.routing_policies order by name""",
    )
    if response.status_code != 200:
        return response
    payload = json.loads(response.body)
    for row in payload["data"]:
        if isinstance(row.get("policy"), str):
            row["policy"] = json.loads(row["policy"])
    return JSONResponse(payload)


@router.get("/usage")
async def usage(request: Request, limit: int = Query(default=100, ge=1, le=500)) -> JSONResponse:
    return await _list_query(
        request,
        """
        select id, request_id, attempt_id, input_tokens, output_tokens, cached_tokens,
               estimated_cost, currency, is_estimate, provider_id_snapshot,
               provider_name_snapshot, provider_model_id_snapshot, route_id_snapshot,
               canonical_model_snapshot, upstream_model_snapshot, protocol_snapshot,
               attempt_status_snapshot, pricing_context, pricing_context_hash, recorded_at
        from public.usage_records
        order by recorded_at desc
        limit $1
        """,
        limit,
    )


@router.get("/requests/{request_id}")
async def request_detail(request_id: str, request: Request) -> JSONResponse:
    claims = await _authorize(request)
    if isinstance(claims, JSONResponse):
        return claims
    pool = _pool(request)
    if pool is None:
        return JSONResponse({"error": "control_plane_unavailable"}, status_code=503)
    row = await pool.fetchrow(
        "select * from public.request_logs where id=$1",
        request_id,
    )
    if row is None:
        return JSONResponse({"error": "request_not_found"}, status_code=404)
    attempts = await pool.fetch(
        """
        select a.id, a.attempt_number, a.provider_id, p.name as provider_name,
               a.credential_id, c.name as credential_name, a.provider_model_id,
               a.status, a.upstream_status, a.error_category, a.response_committed,
               a.started_at, a.ended_at, a.latency_ms
        from public.request_attempts a
        left join public.providers p on p.id=a.provider_id
        left join public.provider_credentials c on c.id=a.credential_id
        where a.request_id=$1 order by a.attempt_number
        """,
        request_id,
    )
    usage_rows = await pool.fetch(
        "select * from public.usage_records where request_id=$1 order by id",
        request_id,
    )
    # The routing trace records every candidate and why it was excluded, but it
    # identifies credentials by id. Resolve those to names so the decision can be
    # read without looking anything up.
    trace = _decode_trace(row.get("routing_trace"))
    credential_names = await _credential_names(pool, _trace_credential_ids(trace))
    return JSONResponse(
        jsonable_encoder(
            {
                "request": dict(row),
                "attempts": [dict(item) for item in attempts],
                "usage": [dict(item) for item in usage_rows],
                "routing": _label_trace(trace, credential_names),
            }
        )
    )


def _decode_trace(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return value if isinstance(value, list) else []


def _trace_credential_ids(trace: list[dict[str, Any]]) -> set[str]:
    ids: set[str] = set()
    for attempt in trace:
        for candidate in attempt.get("considered") or []:
            if candidate.get("credential_id"):
                ids.add(str(candidate["credential_id"]))
        selected = attempt.get("selected") or {}
        if selected.get("credential_id"):
            ids.add(str(selected["credential_id"]))
    return ids


async def _credential_names(pool: Any, ids: set[str]) -> dict[str, str]:
    if not ids:
        return {}
    rows = await pool.fetch(
        "select id::text as id, name from public.provider_credentials where id = any($1::uuid[])",
        sorted(ids),
    )
    return {row["id"]: row["name"] for row in rows}


def _credential_label(entry: dict[str, Any], names: dict[str, str]) -> str | None:
    """None when the exclusion was route wide and no credential was involved."""
    credential_id = entry.get("credential_id")
    if not credential_id:
        return None
    return names.get(str(credential_id), "Unavailable")


def _label_trace(
    trace: list[dict[str, Any]], names: dict[str, str]
) -> list[dict[str, Any]]:
    labelled = []
    for attempt in trace:
        candidates = []
        for candidate in attempt.get("considered") or []:
            entry = dict(candidate)
            entry["credential_name"] = _credential_label(candidate, names)
            candidates.append(entry)
        item = dict(attempt)
        item["considered"] = candidates
        selected = attempt.get("selected")
        if selected:
            chosen = dict(selected)
            chosen["credential_name"] = _credential_label(selected, names)
            item["selected"] = chosen
        labelled.append(item)
    return labelled


@router.get("/requests")
async def requests(request: Request, limit: int = Query(default=50, ge=1, le=200)) -> JSONResponse:
    return await _list_query(
        request,
        """
        select id, client_id, protocol, requested_model, resolved_model, status,
               started_at, ended_at, latency_ms, retry_count, fallback_count, error_category
        from public.request_logs
        order by started_at desc
        limit $1
        """,
        limit,
    )


@router.get("/health")
async def health(request: Request, limit: int = Query(default=50, ge=1, le=200)) -> JSONResponse:
    return await _list_query(
        request,
        """
         select h.id, h.provider_id, p.name as provider_name,
                 h.credential_id, c.name as credential_name, h.status, h.latency_ms,
                 h.error_category,
                 case
                   when h.status in ('healthy','degraded') then 'Evaluated at request time'
                   when h.status in ('rate_limited','cooldown') then 'Temporarily unavailable'
                   else 'Not eligible in this state'
                 end as routing_eligibility,
                 h.checked_at
         from public.health_checks h
         left join public.providers p on p.id = h.provider_id
         left join public.provider_credentials c on c.id = h.credential_id
        order by h.checked_at desc
        limit $1
        """,
        limit,
    )


@router.get("/audit")
async def audit(request: Request, limit: int = Query(default=50, ge=1, le=200)) -> JSONResponse:
    return await _list_query(
        request,
        """
        select a.id, a.actor_id, a.action, a.resource_type, a.resource_id,
               a.metadata, a.created_at,
               coalesce(
                 case a.resource_type
                   when 'provider' then p.name
                   when 'credential' then cr.name
                   when 'client' then cl.name
                   when 'client_key' then k.key_prefix
                   when 'model' then coalesce(m.display_name, a.resource_id)
                   when 'provider_model' then pm.model_id || ' via ' || pmp.name
                   when 'model_route' then mrpm.model_id || ' via ' || mrp.name
                   when 'routing_policy' then rp.name
                   when 'config_version' then 'Snapshot ' || a.resource_id
                 end,
                 '(no longer present)'
               ) as resource_name
        from public.audit_logs a
        left join public.providers p
          on a.resource_type = 'provider' and p.id::text = a.resource_id
        left join public.provider_credentials cr
          on a.resource_type = 'credential' and cr.id::text = a.resource_id
        left join public.gateway_clients cl
          on a.resource_type = 'client' and cl.id::text = a.resource_id
        left join public.gateway_client_keys k
          on a.resource_type = 'client_key' and k.id::text = a.resource_id
        left join public.models m
          on a.resource_type = 'model' and m.id = a.resource_id
        left join public.provider_models pm
          on a.resource_type = 'provider_model' and pm.id::text = a.resource_id
        left join public.providers pmp on pmp.id = pm.provider_id
        left join public.model_routes mr
          on a.resource_type = 'model_route' and mr.id::text = a.resource_id
        left join public.provider_models mrpm on mrpm.id = mr.provider_model_id
        left join public.providers mrp on mrp.id = mrpm.provider_id
        left join public.routing_policies rp
          on a.resource_type = 'routing_policy' and rp.id::text = a.resource_id
        order by a.created_at desc
        limit $1
        """,
        limit,
    )


@router.get("/events")
async def events(request: Request, limit: int = Query(default=100, ge=1, le=500)) -> JSONResponse:
    return await _list_query(
        request,
        """
        select * from (
          select 'provider_event:' || e.id::text as id, e.event_type,
                 p.name as provider_name, c.name as credential_name,
                 e.metadata, e.created_at
          from public.provider_events e
          left join public.providers p on p.id = e.provider_id
          left join public.provider_credentials c on c.id = e.credential_id
          union all
          select 'audit:' || a.id::text as id, a.action as event_type,
                 null::text as provider_name, null::text as credential_name,
                 jsonb_build_object(
                   'resource_type', a.resource_type,
                   'resource_id', a.resource_id
                 ) || a.metadata as metadata,
                 a.created_at
          from public.audit_logs a
        ) activity
        order by created_at desc
        limit $1
        """,
        limit,
    )


@router.get("/analytics")
async def analytics(request: Request, days: int = Query(default=7, ge=1, le=90)) -> JSONResponse:
    claims = await _authorize(request)
    if isinstance(claims, JSONResponse):
        return claims
    pool = _pool(request)
    if pool is None:
        return JSONResponse({"error": "control_plane_unavailable"}, status_code=503)
    daily = await pool.fetch(
        """
        with requests as (
          select date_trunc('day', started_at) as day,
                count(*) as requests,
                count(*) filter (where status = 'succeeded') as succeeded,
                count(*) filter (where status = 'failed') as failed,
                avg(latency_ms) filter (where ended_at is not null) as average_latency_ms,
                percentile_cont(0.95) within group (order by latency_ms)
                  filter (where ended_at is not null) as p95_latency_ms
          from public.request_logs
          where started_at >= current_date - ($1::int - 1)
          group by 1
        ), usage as (
          select date_trunc('day', recorded_at) as day,
                 sum(input_tokens) as input_tokens,
                 sum(output_tokens) as output_tokens,
                 sum(cached_tokens) as cached_tokens,
                 count(*) as usage_records,
                 count(*) filter (where estimated_cost is not null) as priced_records
          from public.usage_records
          where recorded_at >= current_date - ($1::int - 1)
          group by 1
        )
        select coalesce(r.day,u.day) as day,r.requests,r.succeeded,r.failed,
               r.average_latency_ms,r.p95_latency_ms,u.input_tokens,u.output_tokens,
               u.cached_tokens,u.usage_records,u.priced_records
        from requests r full outer join usage u on u.day=r.day
        order by day
        """,
        days,
    )
    by_model = await pool.fetch(
        """
        select coalesce(resolved_model, requested_model) as model,
               count(*) as requests,
               count(*) filter (where status = 'succeeded') as succeeded,
               count(*) filter (where status = 'failed') as failed,
               avg(latency_ms) filter (where ended_at is not null) as average_latency_ms
        from public.request_logs
        where started_at >= current_date - ($1::int - 1)
        group by 1 order by requests desc, model
        """,
        days,
    )
    usage = await pool.fetchrow(
        """
        -- Totals include every attempt, because every attempt was billed. The
        -- failed_* columns separate the part that bought nothing, so retries and
        -- failovers are visible as waste instead of inflating an apparent total.
        select sum(input_tokens) as input_tokens, sum(output_tokens) as output_tokens,
               sum(cached_tokens) as cached_tokens,
                count(*) filter (where estimated_cost is not null) as priced_records,
                count(*) as usage_records,
                count(*) filter (where attempt_status_snapshot <> 'succeeded')
                  as failed_records,
                sum(input_tokens) filter (where attempt_status_snapshot <> 'succeeded')
                  as failed_input_tokens,
                sum(output_tokens) filter (where attempt_status_snapshot <> 'succeeded')
                  as failed_output_tokens
        from public.usage_records
        where recorded_at >= current_date - ($1::int - 1)
        """,
        days,
    )
    costs_by_currency = await pool.fetch(
        """
        select currency, sum(estimated_cost) as estimated_cost,
               sum(estimated_cost) filter (where attempt_status_snapshot = 'succeeded')
                 as succeeded_cost,
               sum(estimated_cost) filter (where attempt_status_snapshot <> 'succeeded')
                 as failed_cost
        from public.usage_records
        where recorded_at >= current_date - ($1::int - 1)
          and estimated_cost is not null and currency is not null
        group by currency order by currency
        """,
        days,
    )
    usage_by_model = await _usage_attribution(pool, days, "canonical_model_snapshot", "model")
    usage_by_provider = await _usage_attribution(pool, days, "provider_name_snapshot", "provider")
    usage_by_route = await _usage_attribution(pool, days, "route_id_snapshot", "route")
    attempts_by_provider = await _attempt_attribution(pool, days, "provider")
    attempts_by_model = await _attempt_attribution(pool, days, "model")
    attempts_by_route = await _attempt_attribution(pool, days, "route")
    failover = await pool.fetchrow(
        """
        select count(*) as requests,
               sum(retry_count) as retries,
               sum(fallback_count) as fallbacks,
               count(*) filter (where retry_count > 0) as requests_with_retries,
               count(*) filter (where fallback_count > 0) as requests_with_fallbacks
        from public.request_logs
        where started_at >= current_date - ($1::int - 1)
        """,
        days,
    )
    return JSONResponse(jsonable_encoder({
        "days": days,
        "daily": [dict(row) for row in daily],
        "by_model": [dict(row) for row in by_model],
        "data": [dict(row) for row in by_model],
        "usage": dict(usage),
        "costs_by_currency": [dict(row) for row in costs_by_currency],
        "usage_by_model": usage_by_model,
        "usage_by_provider": usage_by_provider,
        "usage_by_route": usage_by_route,
        "attempts_by_provider": attempts_by_provider,
        "attempts_by_model": attempts_by_model,
        "attempts_by_route": attempts_by_route,
        "failover": dict(failover),
    }))


async def _usage_attribution(
    pool: Any,
    days: int,
    column: str,
    label: str,
) -> list[dict[str, Any]]:
    allowed = {
        "canonical_model_snapshot",
        "provider_name_snapshot",
        "route_id_snapshot",
    }
    if column not in allowed:
        raise ValueError("Unsupported usage attribution column")
    rows = await pool.fetch(
        f"""
        select coalesce({column}::text, 'Unavailable') as {label},
               sum(input_tokens) as input_tokens,
               sum(output_tokens) as output_tokens,
               sum(cached_tokens) as cached_tokens,
               count(*) as usage_records,
               count(*) filter (where estimated_cost is not null) as priced_records,
               count(*) filter (where attempt_status_snapshot <> 'succeeded')
                 as failed_records,
               sum(input_tokens) filter (where attempt_status_snapshot <> 'succeeded')
                 as failed_input_tokens
        from public.usage_records
        where recorded_at >= current_date - ($1::int - 1)
        group by 1 order by usage_records desc, {label}
        """,
        days,
    )
    return [dict(row) for row in rows]


async def _attempt_attribution(
    pool: Any,
    days: int,
    dimension: str,
) -> list[dict[str, Any]]:
    expressions = {
        "provider": "coalesce(u.provider_name_snapshot,p.name,'Unavailable')",
        "model": (
            "coalesce(u.canonical_model_snapshot,pm.model_id,"
            "r.resolved_model,r.requested_model,'Unavailable')"
        ),
        "route": "coalesce(u.route_id_snapshot::text,'Unavailable')",
    }
    if dimension not in expressions:
        raise ValueError("Unsupported attempt attribution dimension")
    expression = expressions[dimension]
    rows = await pool.fetch(
        f"""
        select {expression} as {dimension}, count(*) as attempts,
               count(*) filter (where a.status='succeeded') as succeeded,
               count(*) filter (where a.status='failed') as failed,
               count(*) filter (where a.status='cancelled') as cancelled,
               avg(a.latency_ms) filter (where a.ended_at is not null) as average_latency_ms,
               percentile_cont(0.95) within group (order by a.latency_ms)
                 filter (where a.ended_at is not null) as p95_latency_ms
        from public.request_attempts a
        join public.request_logs r on r.id=a.request_id
        left join public.usage_records u on u.attempt_id=a.id
        left join public.providers p on p.id=a.provider_id
        left join public.provider_models pm on pm.id=a.provider_model_id
        where a.started_at >= current_date - ($1::int - 1)
        group by 1 order by attempts desc, {dimension}
        """,
        days,
    )
    return [dict(row) for row in rows]


async def _list_query(request: Request, query: str, *args: Any) -> JSONResponse:
    claims = await _authorize(request)
    if isinstance(claims, JSONResponse):
        return claims
    pool = _pool(request)
    if pool is None:
        return JSONResponse({"error": "control_plane_unavailable"}, status_code=503)
    rows = await pool.fetch(query, *args)
    return JSONResponse(jsonable_encoder({"data": [dict(row) for row in rows]}))
