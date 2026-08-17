import json
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from gateway.admin.auth import AdminClaims, SupabaseJWTVerifier

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
               count(c.id) filter (where c.enabled and c.health = 'healthy') as healthy_credentials
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
               count(distinct pm.id) as provider_route_count
        from public.models m
        left join public.model_aliases a on a.model_id = m.id
        left join public.provider_models pm on pm.model_id = m.id and pm.enabled
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
               c.success_count, c.failure_count, c.created_at, c.updated_at
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
               c.created_at, c.updated_at
        from public.gateway_clients c
        left join public.gateway_client_keys k on k.client_id = c.id
        group by c.id
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
                pm.upstream_model_id, pm.protocol, pm.capabilities, pm.priority,
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
        select id, attempt_number, provider_id, credential_id, provider_model_id,
               status, upstream_status, error_category, response_committed,
               started_at, ended_at, latency_ms
        from public.request_attempts where request_id=$1 order by attempt_number
        """,
        request_id,
    )
    usage_rows = await pool.fetch(
        "select * from public.usage_records where request_id=$1 order by id",
        request_id,
    )
    return JSONResponse(
        jsonable_encoder(
            {
                "request": dict(row),
                "attempts": [dict(item) for item in attempts],
                "usage": [dict(item) for item in usage_rows],
            }
        )
    )


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
                 h.error_category, h.checked_at, h.source
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
        select id, actor_id, action, resource_type, resource_id, metadata, created_at
        from public.audit_logs
        order by created_at desc
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
        select sum(input_tokens) as input_tokens, sum(output_tokens) as output_tokens,
               sum(cached_tokens) as cached_tokens,
                count(*) filter (where estimated_cost is not null) as priced_records,
                count(*) as usage_records
        from public.usage_records
        where recorded_at >= current_date - ($1::int - 1)
        """,
        days,
    )
    costs_by_currency = await pool.fetch(
        """
        select currency, sum(estimated_cost) as estimated_cost
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
               count(*) filter (where estimated_cost is not null) as priced_records
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
