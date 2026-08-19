import json
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from gateway.admin.api import _authorize, _pool
from gateway.health.probes import run_health_probes

router = APIRouter(prefix="/api/admin/v1", tags=["admin-operations"])


class PoolMemberInput(BaseModel):
    provider_model_id: UUID
    credential_id: UUID
    enabled: bool = True
    draining: bool = False
    priority: int = Field(default=100, ge=0)
    weight: float = Field(default=1, gt=0)


class PoolInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    model_id: str | None = None
    enabled: bool = True
    strategy: Literal["priority", "weighted", "least_loaded"] = "priority"
    settings: dict[str, Any] = Field(default_factory=dict)
    members: list[PoolMemberInput] | None = None


class PoolMembersInput(BaseModel):
    members: list[PoolMemberInput]


class ManualHealthProbeInput(BaseModel):
    provider_id: UUID
    credential_id: UUID | None = None


@router.post("/health/probe")
async def manual_health_probe(
    request: Request, body: ManualHealthProbeInput
) -> JSONResponse:
    context = await _authorized_pool(request)
    if isinstance(context, JSONResponse):
        return context
    runtime = getattr(request.app.state, "runtime", None)
    limiter = getattr(request.app.state, "health_probe_limiter", None)
    if runtime is None or limiter is None:
        return JSONResponse({"error": "health_probe_unavailable"}, status_code=503)
    summary = await run_health_probes(
        runtime,
        getattr(request.app.state, "health_recorder", None),
        limiter,
        provider_id=str(body.provider_id),
        credential_id=str(body.credential_id) if body.credential_id else None,
        manual=True,
    )
    if not any(summary.values()):
        return JSONResponse({"error": "health_probe_target_not_found"}, status_code=404)
    return JSONResponse(jsonable_encoder(summary))


class BudgetInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    scope_type: Literal["global", "client", "provider", "credential", "model", "route"]
    scope_id: str | None = None
    period: Literal["daily", "monthly"] = "monthly"
    currency: str = Field(min_length=3, max_length=3)
    limit_amount: float = Field(ge=0)
    warning_threshold: float = Field(gt=0, le=1, default=0.8)
    enforcement: Literal["warn", "block"] = "warn"
    enabled: bool = True


class AlertRuleInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    enabled: bool = True
    severity: Literal["info", "warning", "critical"] = "warning"
    event_type: str = Field(min_length=1, max_length=120)
    scope_type: str | None = None
    scope_id: str | None = None
    condition: dict[str, Any] = Field(default_factory=dict)
    cooldown_seconds: int = Field(ge=0, default=300)


async def _authorized_pool(request: Request) -> tuple[Any, Any] | JSONResponse:
    claims = await _authorize(request)
    if isinstance(claims, JSONResponse):
        return claims
    pool = _pool(request)
    if pool is None:
        return JSONResponse({"error": "control_plane_unavailable"}, status_code=503)
    return claims, pool


@router.get("/provider-pools")
async def list_pools(request: Request) -> JSONResponse:
    context = await _authorized_pool(request)
    if isinstance(context, JSONResponse):
        return context
    _, pool = context
    rows = await pool.fetch(
        """
        select p.id,p.name,p.model_id,p.enabled,p.strategy,p.settings,p.created_at,p.updated_at,
               count(m.credential_id) as member_count,
               count(m.credential_id) filter (
                 where m.enabled and not m.draining
               ) as routable_members
        from public.provider_pools p left join public.provider_pool_members m on m.pool_id=p.id
        group by p.id order by p.name
        """
    )
    return JSONResponse(jsonable_encoder({"data": [dict(row) for row in rows]}))


@router.post("/provider-pools")
async def create_pool(request: Request, body: PoolInput) -> JSONResponse:
    context = await _authorized_pool(request)
    if isinstance(context, JSONResponse):
        return context
    claims, pool = context
    row = await pool.fetchrow(
        """insert into public.provider_pools(name,model_id,enabled,strategy,settings)
           values($1,$2,$3,$4,$5::jsonb) returning *""",
        body.name,
        body.model_id,
        body.enabled,
        body.strategy,
        json.dumps(body.settings),
    )
    await _insert_pool_members(pool, row["id"], body.members or [])
    await pool.execute(
        """insert into public.audit_logs(actor_id,action,resource_type,resource_id,metadata)
           values($1,'provider_pool_created','provider_pool',$2,$3::jsonb)""",
        claims.subject,
        str(row["id"]),
        json.dumps({"changed_fields": sorted(body.model_fields_set)}),
    )
    return JSONResponse(jsonable_encoder(dict(row)), status_code=201)


@router.put("/provider-pools/{pool_id}")
async def update_pool(pool_id: UUID, request: Request, body: PoolInput) -> JSONResponse:
    context = await _authorized_pool(request)
    if isinstance(context, JSONResponse):
        return context
    claims, pool = context
    row = await pool.fetchrow(
        """update public.provider_pools set name=$2,model_id=$3,enabled=$4,strategy=$5,
             settings=$6::jsonb,updated_at=now() where id=$1 returning *""",
        pool_id,
        body.name,
        body.model_id,
        body.enabled,
        body.strategy,
        json.dumps(body.settings),
    )
    if row is None:
        return JSONResponse({"error": "provider_pool_not_found"}, status_code=404)
    if body.members is not None:
        await pool.execute("delete from public.provider_pool_members where pool_id=$1", pool_id)
        await _insert_pool_members(pool, pool_id, body.members)
    await pool.execute(
        """insert into public.audit_logs(actor_id,action,resource_type,resource_id)
           values($1,'provider_pool_updated','provider_pool',$2)""",
        claims.subject,
        str(pool_id),
    )
    return JSONResponse(jsonable_encoder(dict(row)))


async def _insert_pool_members(
    pool: Any, pool_id: UUID, members: list[PoolMemberInput]
) -> None:
    for member in members:
        valid = await pool.fetchval(
            """select 1 from public.provider_models pm
               join public.provider_credentials c on c.id=$2
               where pm.id=$1 and pm.provider_id=c.provider_id""",
            member.provider_model_id,
            member.credential_id,
        )
        if valid is None:
            raise HTTPException(
                status_code=422,
                detail="pool member mapping and credential must share a provider",
            )
        await pool.execute(
            """insert into public.provider_pool_members(
              pool_id,provider_model_id,credential_id,enabled,draining,priority,weight)
              values($1,$2,$3,$4,$5,$6,$7)""",
            pool_id,
            member.provider_model_id,
            member.credential_id,
            member.enabled,
            member.draining,
            member.priority,
            member.weight,
        )


@router.delete("/provider-pools/{pool_id}")
async def delete_pool(pool_id: UUID, request: Request) -> JSONResponse:
    context = await _authorized_pool(request)
    if isinstance(context, JSONResponse):
        return context
    claims, pool = context
    row = await pool.fetchrow("delete from public.provider_pools where id=$1 returning id", pool_id)
    if row is None:
        return JSONResponse({"error": "provider_pool_not_found"}, status_code=404)
    await pool.execute(
        """insert into public.audit_logs(actor_id,action,resource_type,resource_id)
           values($1,'provider_pool_deleted','provider_pool',$2)""",
        claims.subject,
        str(pool_id),
    )
    return JSONResponse({"deleted": True})


@router.put("/provider-pools/{pool_id}/members")
async def replace_pool_members(
    pool_id: UUID, body: PoolMembersInput, request: Request
) -> JSONResponse:
    context = await _authorized_pool(request)
    if isinstance(context, JSONResponse):
        return context
    claims, pool = context
    if await pool.fetchval("select 1 from public.provider_pools where id=$1", pool_id) is None:
        return JSONResponse({"error": "provider_pool_not_found"}, status_code=404)
    await pool.execute("delete from public.provider_pool_members where pool_id=$1", pool_id)
    await _insert_pool_members(pool, pool_id, body.members)
    await pool.execute(
        """insert into public.audit_logs(actor_id,action,resource_type,resource_id,metadata)
           values($1,'provider_pool_members_replaced','provider_pool',$2,$3::jsonb)""",
        claims.subject,
        str(pool_id),
        json.dumps({"member_count": len(body.members)}),
    )
    return JSONResponse({"pool_id": str(pool_id), "member_count": len(body.members)})


@router.get("/provider-models/{provider_model_id}/credentials")
async def mapping_credentials(provider_model_id: str, request: Request) -> JSONResponse:
    context = await _authorized_pool(request)
    if isinstance(context, JSONResponse):
        return context
    _, pool = context
    rows = await pool.fetch(
        """select c.id,c.name,c.provider_id,c.enabled,c.priority,c.health,
                  c.requests_per_minute,c.tokens_per_minute,
                  (cma.provider_model_id is not null) as assigned
           from public.provider_credentials c
           left join public.credential_model_access cma
             on cma.credential_id=c.id and cma.provider_model_id=$1
           where c.provider_id=(select provider_id from public.provider_models where id=$1)
           order by c.priority,c.name""",
        provider_model_id,
    )
    return JSONResponse(jsonable_encoder({"data": [dict(row) for row in rows]}))


@router.get("/budgets")
async def list_budgets(request: Request) -> JSONResponse:
    context = await _authorized_pool(request)
    if isinstance(context, JSONResponse):
        return context
    _, pool = context
    rows = await pool.fetch(
        """select b.*,
                  coalesce(sum(w.reserved_cost) filter (
                    where w.window_started_at = case b.period
                      when 'daily' then date_trunc('day', now())
                      else date_trunc('month', now()) end
                  ),0) as used,
                  coalesce(sum(w.request_count) filter (
                    where w.window_started_at = case b.period
                      when 'daily' then date_trunc('day', now())
                      else date_trunc('month', now()) end
                  ),0) as requests_this_period,
                  coalesce(sum(w.reserved_cost),0) as used_all_time,
                  case b.period
                    when 'daily' then date_trunc('day', now())
                    else date_trunc('month', now()) end as current_window_started_at
           from public.gateway_budgets b left join public.budget_usage_windows w on w.budget_id=b.id
           group by b.id order by b.name"""
    )
    return JSONResponse(jsonable_encoder({"data": [dict(row) for row in rows]}))


@router.post("/budgets")
async def create_budget(request: Request, body: BudgetInput) -> JSONResponse:
    context = await _authorized_pool(request)
    if isinstance(context, JSONResponse):
        return context
    claims, pool = context
    row = await pool.fetchrow(
        """insert into public.gateway_budgets(name,scope_type,scope_id,period,currency,
             limit_amount,warning_threshold,enforcement,enabled)
           values($1,$2,$3,$4,$5,$6,$7,$8,$9) returning *""",
        body.name,
        body.scope_type,
        body.scope_id,
        body.period,
        body.currency.upper(),
        body.limit_amount,
        body.warning_threshold,
        body.enforcement,
        body.enabled,
    )
    await pool.execute(
        """insert into public.audit_logs(actor_id,action,resource_type,resource_id,metadata)
           values($1,'budget_created','budget',$2,$3::jsonb)""",
        claims.subject,
        str(row["id"]),
        json.dumps({"changed_fields": sorted(body.model_fields_set)}),
    )
    return JSONResponse(jsonable_encoder(dict(row)), status_code=201)


@router.put("/budgets/{budget_id}")
async def update_budget(budget_id: UUID, request: Request, body: BudgetInput) -> JSONResponse:
    context = await _authorized_pool(request)
    if isinstance(context, JSONResponse):
        return context
    claims, pool = context
    row = await pool.fetchrow(
        """update public.gateway_budgets set name=$2,scope_type=$3,scope_id=$4,period=$5,
             currency=$6,limit_amount=$7,warning_threshold=$8,enforcement=$9,enabled=$10,
             updated_at=now() where id=$1 returning *""",
        budget_id,
        body.name,
        body.scope_type,
        body.scope_id,
        body.period,
        body.currency.upper(),
        body.limit_amount,
        body.warning_threshold,
        body.enforcement,
        body.enabled,
    )
    if row is None:
        return JSONResponse({"error": "budget_not_found"}, status_code=404)
    await pool.execute(
        """insert into public.audit_logs(actor_id,action,resource_type,resource_id)
           values($1,'budget_updated','budget',$2)""",
        claims.subject,
        str(budget_id),
    )
    return JSONResponse(jsonable_encoder(dict(row)))


@router.delete("/budgets/{budget_id}")
async def delete_budget(budget_id: UUID, request: Request) -> JSONResponse:
    context = await _authorized_pool(request)
    if isinstance(context, JSONResponse):
        return context
    claims, pool = context
    row = await pool.fetchrow(
        "delete from public.gateway_budgets where id=$1 returning id", budget_id
    )
    if row is None:
        return JSONResponse({"error": "budget_not_found"}, status_code=404)
    await pool.execute(
        """insert into public.audit_logs(actor_id,action,resource_type,resource_id)
           values($1,'budget_deleted','budget',$2)""",
        claims.subject,
        str(budget_id),
    )
    return JSONResponse({"deleted": True})


@router.get("/alerts")
async def list_alerts(request: Request, status: str | None = Query(default=None)) -> JSONResponse:
    context = await _authorized_pool(request)
    if isinstance(context, JSONResponse):
        return context
    _, pool = context
    if status:
        rows = await pool.fetch(
            "select * from public.alerts where status=$1 order by last_seen_at desc limit 500",
            status,
        )
    else:
        rows = await pool.fetch("select * from public.alerts order by last_seen_at desc limit 500")
    return JSONResponse(jsonable_encoder({"data": [dict(row) for row in rows]}))


@router.get("/alert-rules")
async def list_alert_rules(request: Request) -> JSONResponse:
    context = await _authorized_pool(request)
    if isinstance(context, JSONResponse):
        return context
    _, pool = context
    rows = await pool.fetch("select * from public.alert_rules order by name")
    return JSONResponse(jsonable_encoder({"data": [dict(row) for row in rows]}))


@router.post("/alert-rules")
async def create_alert_rule(request: Request, body: AlertRuleInput) -> JSONResponse:
    context = await _authorized_pool(request)
    if isinstance(context, JSONResponse):
        return context
    claims, pool = context
    row = await pool.fetchrow(
        """insert into public.alert_rules(
             name,enabled,severity,event_type,scope_type,scope_id,condition,cooldown_seconds)
           values($1,$2,$3,$4,$5,$6,$7::jsonb,$8) returning *""",
        body.name,
        body.enabled,
        body.severity,
        body.event_type,
        body.scope_type,
        body.scope_id,
        json.dumps(body.condition),
        body.cooldown_seconds,
    )
    await pool.execute(
        """insert into public.audit_logs(actor_id,action,resource_type,resource_id,metadata)
           values($1,'alert_rule_created','alert_rule',$2,$3::jsonb)""",
        claims.subject,
        str(row["id"]),
        json.dumps({"changed_fields": sorted(body.model_fields_set)}),
    )
    return JSONResponse(jsonable_encoder(dict(row)), status_code=201)


@router.put("/alert-rules/{rule_id}")
async def update_alert_rule(rule_id: UUID, request: Request, body: AlertRuleInput) -> JSONResponse:
    context = await _authorized_pool(request)
    if isinstance(context, JSONResponse):
        return context
    claims, pool = context
    row = await pool.fetchrow(
        """update public.alert_rules set name=$2,enabled=$3,severity=$4,event_type=$5,
             scope_type=$6,scope_id=$7,condition=$8::jsonb,cooldown_seconds=$9,
             updated_at=now() where id=$1 returning *""",
        rule_id,
        body.name,
        body.enabled,
        body.severity,
        body.event_type,
        body.scope_type,
        body.scope_id,
        json.dumps(body.condition),
        body.cooldown_seconds,
    )
    if row is None:
        return JSONResponse({"error": "alert_rule_not_found"}, status_code=404)
    await pool.execute(
        """insert into public.audit_logs(actor_id,action,resource_type,resource_id)
           values($1,'alert_rule_updated','alert_rule',$2)""",
        claims.subject,
        str(rule_id),
    )
    return JSONResponse(jsonable_encoder(dict(row)))


@router.delete("/alert-rules/{rule_id}")
async def delete_alert_rule(rule_id: UUID, request: Request) -> JSONResponse:
    context = await _authorized_pool(request)
    if isinstance(context, JSONResponse):
        return context
    claims, pool = context
    row = await pool.fetchrow("delete from public.alert_rules where id=$1 returning id", rule_id)
    if row is None:
        return JSONResponse({"error": "alert_rule_not_found"}, status_code=404)
    await pool.execute(
        """insert into public.audit_logs(actor_id,action,resource_type,resource_id)
           values($1,'alert_rule_deleted','alert_rule',$2)""",
        claims.subject,
        str(rule_id),
    )
    return JSONResponse({"deleted": True})


@router.post("/alerts/{alert_id}/acknowledge")
async def acknowledge_alert(alert_id: int, request: Request) -> JSONResponse:
    return await _set_alert_status(alert_id, "acknowledged", request)


@router.post("/alerts/{alert_id}/resolve")
async def resolve_alert(alert_id: int, request: Request) -> JSONResponse:
    return await _set_alert_status(alert_id, "resolved", request)


async def _set_alert_status(alert_id: int, status: str, request: Request) -> JSONResponse:
    context = await _authorized_pool(request)
    if isinstance(context, JSONResponse):
        return context
    claims, pool = context
    row = await pool.fetchrow(
        """update public.alerts set status=$2,acknowledged_by=$3,
             acknowledged_at=case when $2='acknowledged' then now()
               else acknowledged_at end,
             resolved_at=case when $2='resolved' then now() else resolved_at end
           where id=$1 returning *""",
        alert_id,
        status,
        claims.subject,
    )
    if row is None:
        return JSONResponse({"error": "alert_not_found"}, status_code=404)
    return JSONResponse(jsonable_encoder(dict(row)))
