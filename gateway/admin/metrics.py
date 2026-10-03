import asyncio
from dataclasses import asdict
from decimal import Decimal
from math import isfinite
from time import monotonic

from fastapi import APIRouter, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from gateway.admin.api import _authorize
from gateway.distributed_capacity import CapacityUnavailable, DistributedCapacity
from gateway.metrics import metrics
from gateway.operating_envelope import OperatingEnvelopePolicy, evaluate_operating_envelope

router = APIRouter(prefix="/api/admin/v1", tags=["admin"])


class CapacityPolicyInput(BaseModel):
    scope: str = Field(min_length=1, max_length=200)
    capacity: int = Field(ge=1, le=10000)
    version: int = Field(ge=0)


@router.post("/capacity")
async def capacity_policy(request: Request, body: CapacityPolicyInput):
    claims = await _authorize(request)
    if isinstance(claims, JSONResponse):
        return claims
    if request.app.state.db_pool is None:
        return JSONResponse({"error": "coordination_unavailable"}, status_code=503)
    try:
        return await DistributedCapacity(request.app.state.db_pool).configure(
            body.scope, body.capacity, body.version,
        )
    except CapacityUnavailable as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)


@router.get("/metrics")
async def stage_metrics(request: Request):
    claims = await _authorize(request)
    if isinstance(claims, JSONResponse):
        return claims
    return JSONResponse(metrics.snapshot())


@router.get("/traces")
async def stage_traces(request: Request):
    claims = await _authorize(request)
    if isinstance(claims, JSONResponse):
        return claims
    return JSONResponse({"scope": "process", "samples": list(metrics.traces)})


@router.post("/drain")
async def drain_process(request: Request):
    claims = await _authorize(request)
    if isinstance(claims, JSONResponse):
        return claims
    drain = request.app.state.drain
    complete = await drain.drain(request.app.state.settings.shutdown_timeout_seconds)
    return JSONResponse({"draining": True, "active": drain.active, "complete": complete},
                        status_code=200 if complete else 202)


def _measurement(value):
    # asyncpg epoch/numeric aggregates are Decimal; absent optional fields are unknown.
    if type(value) not in (int, float, Decimal):
        return None
    try:
        value = float(value)
    except (OverflowError, ValueError):
        return None
    return value if isfinite(value) and value >= 0 else None


def _utilization(used, maximum):
    used, maximum = _measurement(used), _measurement(maximum)
    if used is None or maximum is None or maximum == 0:
        return None
    return min(1.0, used / maximum)


def _attach_envelope(request, result, started, active_utilization):
    state = request.app.state
    supplied = getattr(state, "operating_envelope_policy", None)
    policy = supplied if supplied is not None else OperatingEnvelopePolicy()
    observed = {"active_request_utilization": active_utilization}
    pool = result.get("pool")
    if pool:
        observed["db_pool_utilization"] = _utilization(
            pool["size"] - pool["idle"], pool["maximum"],
        )
    if result["database"] == "available":
        observed["dependency_healthy"] = True  # Database only, not upstream health.
        if "workflows" in result:
            queued = [row for row in result["workflows"] if row.get("status") == "queued"]
            ages = [_measurement(row.get("oldest_seconds")) for row in queued]
            observed["queue_age_seconds"] = (
                max(ages, default=0) if all(age is not None for age in ages) else None
            )
        accounting = result.get("accounting", {})
        for name in ("unknown_usage", "settlement_backlog"):
            value = accounting.get(name)
            observed[name] = value if type(value) is int and value >= 0 else None
        observed["oldest_settlement_seconds"] = _measurement(
            accounting.get("oldest_settlement_seconds"),
        )
        provider = _measurement(result.get("capacity", {}).get("provider_utilization"))
        observed["provider_utilization"] = min(1.0, provider) if provider is not None else None
    elif result["database"] == "unavailable":
        observed["dependency_healthy"] = False
    # There is no timestamped recent lag or successful config-refresh signal here.
    # In particular, a lifetime histogram maximum is not current event-loop lag.
    envelope = evaluate_operating_envelope(observed, policy)
    missing = {name: "not_collected_in_this_observation"
               for name, entry in envelope["statuses"].items() if entry["status"] == "unknown"}
    missing.update(config_age_seconds="successful_config_refresh_time_unavailable",
                   event_loop_lag_seconds="recent_lag_window_unavailable")
    if "provider_utilization" in missing:
        missing["provider_utilization"] = "provider_capacity_disabled_unconfigured_or_unobserved"
    if supplied is None:
        missing["current_replicas"] = "deployment_replica_count_unavailable"
        envelope["reasons"].append("current_replicas_unknown")
        envelope["recommendation"] = "investigate"
    envelope.update(
        advisory_only=True, scale_out_authorized=False,
        scope={"process": "local", "readiness": "process_and_database_only",
               "deployment": "unknown", "dependency_healthy": "database_only",
               "provider_utilization": "peak_registered_provider_lease_utilization"},
        readiness={"process_ready": bool(result["ready"] and not result["draining"]),
                   "database": result["database"]},
        observation={"window_seconds": monotonic() - started, "query_budget_seconds": 2,
                     "consistency": "sequential_reads_within_request_not_atomic"},
        policy={"source": "app.state.operating_envelope_policy" if supplied is not None
                else "provisional_default", "values": asdict(policy)},
        current_replicas={"value": policy.current_replicas if supplied is not None else None,
                          "source": "supplied_policy" if supplied is not None else "unknown"},
        connection_budget_basis="policy_estimate_not_measured_deployment",
        missing_signal_reasons=missing,
    )
    result["operating_envelope"] = envelope
    return JSONResponse(jsonable_encoder(result))


@router.get("/operations/health")
async def operational_health(request: Request):
    claims = await _authorize(request)
    if isinstance(claims, JSONResponse):
        return claims
    started = monotonic()
    pool = request.app.state.db_pool
    result = {"ready": request.app.state.ready, "draining": request.app.state.drain.draining,
              "active_requests": request.app.state.drain.active, "warnings": []}
    active_utilization = _utilization(result["active_requests"], getattr(
        request.app.state.settings, "max_in_flight_requests", None,
    ))
    recorder = request.app.state.health_recorder
    if recorder is not None:
        result["passive_health"] = {"dropped": recorder.dropped_events,
                                    "write_failures": recorder.write_failures}
        if recorder.dropped_events:
            result["warnings"].append("health_events_dropped")
    if pool is None:
        result["database"] = "unconfigured"
        return _attach_envelope(request, result, started, active_utilization)
    result["pool"] = {"size": pool.get_size(), "idle": pool.get_idle_size(),
                      "maximum": pool.get_max_size()}
    if result["pool"]["idle"] == 0 and result["pool"]["size"] >= result["pool"]["maximum"]:
        result["warnings"].append("pool_exhausted")
    try:
        # One budget covers acquisition and all queries, including feature-disabled deployments.
        async with asyncio.timeout(2):
            probe = await pool.fetchval("select 1")
            if type(probe) is not int or probe != 1:
                raise RuntimeError("Invalid database probe")
            if request.app.state.settings.workflows_enabled:
                rows = await pool.fetch(
                    "select status,count(*) as count,"
                    "max(extract(epoch from now()-case when status='queued' then updated_at "
                    "else created_at end)) as oldest_seconds "
                    "from public.workflow_jobs "
                    "where status in ('queued','running',"
                    "'waiting_for_tool','waiting_for_approval') "
                    "group by status"
                )
                result["workflows"] = [dict(row) for row in rows]
                if any(row.get("status") == "queued"
                       and (_measurement(row.get("oldest_seconds")) or 0) > 60
                       for row in result["workflows"]):
                    result["warnings"].append("workflow_queue_age")
            if request.app.state.settings.durable_accounting_enabled:
                row = await pool.fetchrow(
                    "select count(*) filter(where status='unknown') as unknown_usage,"
                    "count(*) filter(where status='settlement_pending') as settlement_backlog,"
                    "(select coalesce(greatest(0,extract(epoch from now()-min(created_at))),0) "
                    "from public.settlement_outbox where applied_at is null) "
                    "as oldest_settlement_seconds "
                    "from public.financial_reservations where status<>'settled'"
                )
                result["accounting"] = dict(row) if row is not None else {}
                if result["accounting"].get("unknown_usage"):
                    result["warnings"].append("unknown_usage")
                if result["accounting"].get("settlement_backlog"):
                    result["warnings"].append("settlement_backlog")
            settings = request.app.state.settings
            if (getattr(settings, "distributed_admission_enabled", False)
                    and _measurement(getattr(settings, "distributed_provider_capacity", None))):
                row = await pool.fetchrow(
                    "select max(used * 1.0 / capacity) as provider_utilization from ("
                    "select s.scope,least(s.capacity,$1) as capacity,count(l.owner) as used "
                    "from public.capacity_scopes s left join public.capacity_leases l "
                    "on l.scope=s.scope and l.expires_at>now() "
                    "where s.scope like 'provider:%' group by s.scope,s.capacity) as providers",
                    settings.distributed_provider_capacity,
                )
                result["capacity"] = dict(row) if row is not None else {}
        result["database"] = "available"
    except Exception:
        result["database"] = "unavailable"
        result["warnings"].append("database_unavailable")
    return _attach_envelope(request, result, started, active_utilization)
