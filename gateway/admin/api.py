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
    return getattr(request.app.state, "db_pool", None)


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
          coalesce(avg(latency_ms) filter (where started_at >= current_date), 0) as avg_latency_ms,
          coalesce(
            avg((fallback_count > 0)::int) filter (where started_at >= current_date), 0
          ) as fallback_rate
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
        select coalesce(sum(input_tokens), 0) as input_tokens,
               coalesce(sum(output_tokens), 0) as output_tokens,
               coalesce(sum(estimated_cost), 0) as estimated_cost
        from public.usage_records
        where recorded_at >= date_trunc('month', now())
        """
    )
    return JSONResponse(
        jsonable_encoder(
            {
                **dict(row),
                **dict(provider_row),
                **dict(key_row),
                **dict(cost_row),
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
                 array_agg(distinct a.alias) filter (where a.alias is not null), '{}'
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
               r.allow_model_fallback, r.policy_id, rp.name as policy_name, r.created_at
        from public.model_routes r
        join public.provider_models pm on pm.id = r.provider_model_id
        join public.providers p on p.id = pm.provider_id
        left join public.routing_policies rp on rp.id = r.policy_id
        order by r.model_id, r.priority
        """,
    )


@router.get("/provider-models")
async def provider_models(request: Request) -> JSONResponse:
    return await _list_query(
        request,
        """
        select pm.id, pm.provider_id, p.name as provider_name, pm.model_id,
                pm.upstream_model_id, pm.protocol, pm.capabilities, pm.priority,
                pm.weight, pm.max_concurrency, pm.enabled, pm.pricing,
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
               estimated_cost, currency, is_estimate, recorded_at
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
        select h.id, h.provider_id, h.credential_id, h.status, h.latency_ms,
               h.error_category, h.checked_at
        from public.health_checks h
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


async def _list_query(request: Request, query: str, *args: Any) -> JSONResponse:
    claims = await _authorize(request)
    if isinstance(claims, JSONResponse):
        return claims
    pool = _pool(request)
    if pool is None:
        return JSONResponse({"error": "control_plane_unavailable"}, status_code=503)
    rows = await pool.fetch(query, *args)
    return JSONResponse(jsonable_encoder({"data": [dict(row) for row in rows]}))
