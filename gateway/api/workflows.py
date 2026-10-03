from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from gateway.auth.service import authenticate_key
from gateway.protocols import ClientProtocol
from gateway.workflow_store import WorkflowConflict, WorkflowStore

router = APIRouter(prefix="/v1/workflows", tags=["workflows"])


class WorkflowInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol: ClientProtocol
    request: dict
    deadline_seconds: int = Field(default=3600, ge=1, le=86400)
    # Tools are executed externally; worker returns a wait checkpoint, never runs arbitrary code.
    max_steps: int = Field(default=4, ge=1, le=8)
    max_tool_calls: int = Field(default=16, ge=0, le=64)
    max_total_tokens: int = Field(default=131072, ge=1, le=1048576)
    max_cost: Decimal = Field(default=Decimal("10"), gt=0, le=1000, allow_inf_nan=False)
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")


class ResumeInput(BaseModel):
    version: int = Field(ge=0)


class ToolResultInput(BaseModel):
    invocation_id: str = Field(min_length=1, max_length=200)
    result: dict | list | str


def context(request):
    if not request.app.state.settings.workflows_enabled:
        return JSONResponse({"error": "workflows_disabled"}, status_code=404)
    runtime = request.app.state.runtime
    if runtime is None or request.app.state.db_pool is None:
        return JSONResponse({"error": "workflow_service_unavailable"}, status_code=503)
    authenticated = authenticate_key(request.headers, store=runtime.key_store,
                                     hasher=runtime.key_hasher)
    if authenticated is None:
        return JSONResponse({"error": "invalid_gateway_key"}, status_code=401)
    settings = request.app.state.settings
    return WorkflowStore(request.app.state.db_pool,
                         per_client_capacity=settings.workflow_per_client_capacity,
                         max_pending=settings.workflow_max_pending_per_client), authenticated.client


def public_job(job):
    if job is None:
        return JSONResponse({"error": "workflow_not_found"}, status_code=404)
    keys = ("id", "status", "version", "checkpoint", "result", "deadline", "created_at",
            "updated_at", "reserved_tokens", "reserved_cost")
    return {key: job[key] for key in keys if key in job}


@router.post("")
async def submit(request: Request, body: WorkflowInput):
    ctx = context(request)
    if isinstance(ctx, Response):
        return ctx
    store, client = ctx
    model = body.request.get("model")
    if not isinstance(model, str) or not client.permissions.permits(body.protocol, model):
        return JSONResponse({"error": "model_or_protocol_not_permitted"}, status_code=403)
    if body.request.get("stream"):
        return JSONResponse({"error": "workflow_requests_must_be_non_streaming"}, status_code=400)
    if body.request.get("background") not in (None, False):
        return JSONResponse({"error": "provider_background_execution_unsupported"}, status_code=400)
    try:
        job = await store.submit(client.id, request.headers.get("idempotency-key", ""),
                                 body.model_dump(mode="json"), body.deadline_seconds)
    except WorkflowConflict as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(jsonable_encoder(public_job(job)), status_code=202)


@router.get("/{job_id}")
async def status(job_id: UUID, request: Request):
    ctx = context(request)
    if isinstance(ctx, Response):
        return ctx
    store, client = ctx
    return public_job(await store.get(client.id, job_id))


@router.post("/{job_id}/cancel")
async def cancel(job_id: UUID, request: Request):
    ctx = context(request)
    if isinstance(ctx, Response):
        return ctx
    store, client = ctx
    return public_job(await store.cancel(client.id, job_id))


@router.post("/{job_id}/resume")
async def resume(job_id: UUID, request: Request, body: ResumeInput):
    ctx = context(request)
    if isinstance(ctx, Response):
        return ctx
    store, client = ctx
    if await store.get(client.id, job_id) is None:
        return public_job(None)
    try:
        return public_job(await store.resume(client.id, job_id, body.version))
    except WorkflowConflict as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)


@router.post("/{job_id}/tool-results")
async def tool_result(job_id: UUID, request: Request, body: ToolResultInput):
    ctx = context(request)
    if isinstance(ctx, Response):
        return ctx
    store, client = ctx
    try:
        if isinstance(body.result, dict) and set(body.result) == {"artifact_id"}:
            from gateway.api.artifacts import store as artifact_store

            artifacts = artifact_store(request)
            if artifacts is None:
                return JSONResponse({"error": "artifact_storage_not_configured"}, status_code=503)
            raw_id = body.result["artifact_id"]
            if not isinstance(raw_id, str):
                raise ValueError("Artifact ID must be a UUID string")
            artifact_id = UUID(raw_id)
            content = await artifacts.get(client.id, job_id, artifact_id)
            if content is None:
                return JSONResponse({"error": "artifact_not_found"}, status_code=404)
        job = await store.tool_result(client.id, job_id, body.invocation_id, body.result)
        return public_job(job)
    except WorkflowConflict as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@router.get("/{job_id}/events")
async def events(job_id: UUID, request: Request):
    ctx = context(request)
    if isinstance(ctx, Response):
        return ctx
    store, client = ctx
    if await store.get(client.id, job_id) is None:
        return public_job(None)
    try:
        after = int(request.headers.get("last-event-id", "-1"))
        if after < -1 or after > 9223372036854775807:
            raise ValueError
    except ValueError:
        return JSONResponse({"error": "invalid_last_event_id"}, status_code=400)
    # Bounded replay page. Clients reconnect with Last-Event-ID; replay never executes jobs.
    from gateway.workflow_store import encode

    rows = await store.events(client.id, job_id, after)
    content = "".join(f"id: {row['sequence']}\nevent: {row['kind']}\n"
                      f"data: {encode(row['data'])}\n\n" for row in rows)
    return Response(content, media_type="text/event-stream", headers={"Cache-Control": "no-store"})
