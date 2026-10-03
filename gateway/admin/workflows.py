"""Operator-facing, read-only workflow inspection.

The client workflow API (gateway/api/workflows.py) authenticates with a client
gateway key and is scoped to a single tenant. This router authenticates with an
operator's admin session instead, so the dashboard can list and inspect jobs across
all tenants. It is deliberately read-only: it never claims, transitions, cancels or
resumes a job, so it cannot race the workers that own those transitions.
"""

import contextlib
import json
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from gateway.admin.api import _authorize, _pool

router = APIRouter(prefix="/api/admin/v1", tags=["admin-workflows"])

STATUSES = (
    "queued", "running", "waiting_for_tool", "waiting_for_approval",
    "completed", "failed", "cancelled", "expired",
)

# The public-facing columns for the list. Idempotency keys and input/checkpoint
# hashes are operational plumbing, so they are omitted here; the detail view carries
# the checkpoint and result an operator actually needs to diagnose a stuck job.
LIST_COLUMNS = (
    "id", "client_id", "status", "version", "attempts", "max_attempts",
    "deadline", "created_at", "updated_at",
)


def _decode(row: dict[str, Any], *columns: str) -> dict[str, Any]:
    for column in columns:
        value = row.get(column)
        if isinstance(value, str):
            with contextlib.suppress(ValueError):
                row[column] = json.loads(value)
    return row


def _unavailable(request: Request) -> JSONResponse | None:
    if not request.app.state.settings.workflows_enabled:
        return JSONResponse({"error": "workflows_disabled"}, status_code=404)
    if _pool(request) is None:
        return JSONResponse({"error": "workflow_service_unavailable"}, status_code=503)
    return None


@router.get("/workflows")
async def list_workflows(
    request: Request,
    status: Annotated[str | None, Query()] = None,
    client_id: Annotated[UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> JSONResponse:
    claims = await _authorize(request)
    if isinstance(claims, JSONResponse):
        return claims
    unavailable = _unavailable(request)
    if unavailable is not None:
        return unavailable
    if status is not None and status not in STATUSES:
        return JSONResponse({"error": "invalid_status"}, status_code=400)
    pool = _pool(request)
    columns = ",".join(f"j.{name}" for name in LIST_COLUMNS)
    clauses = []
    args: list[Any] = []
    if status is not None:
        args.append(status)
        clauses.append(f"j.status=${len(args)}")
    if client_id is not None:
        args.append(client_id)
        clauses.append(f"j.client_id=${len(args)}")
    where = f"where {' and '.join(clauses)}" if clauses else ""
    args.append(limit)
    rows = await pool.fetch(
        f"select {columns},c.name as client_name "
        f"from public.workflow_jobs j "
        f"join public.gateway_clients c on c.id=j.client_id "
        f"{where} order by j.created_at desc limit ${len(args)}",
        *args,
    )
    return JSONResponse(jsonable_encoder({"data": [dict(row) for row in rows]}))


@router.get("/workflows/{job_id}")
async def workflow_detail(job_id: UUID, request: Request) -> JSONResponse:
    claims = await _authorize(request)
    if isinstance(claims, JSONResponse):
        return claims
    unavailable = _unavailable(request)
    if unavailable is not None:
        return unavailable
    pool = _pool(request)
    row = await pool.fetchrow(
        "select j.*,c.name as client_name from public.workflow_jobs j "
        "join public.gateway_clients c on c.id=j.client_id where j.id=$1",
        job_id,
    )
    if row is None:
        return JSONResponse({"error": "workflow_not_found"}, status_code=404)
    job = _decode(dict(row), "input", "checkpoint", "result")
    events = await pool.fetch(
        "select sequence,kind,data,created_at from public.workflow_events "
        "where job_id=$1 order by sequence desc limit 200",
        job_id,
    )
    job["events"] = [_decode(dict(event), "data") for event in events]
    return JSONResponse(jsonable_encoder(job))
