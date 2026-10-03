import base64
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from gateway.api.workflows import context
from gateway.artifacts import ArtifactStore

router = APIRouter(prefix="/v1/workflows", tags=["workflows"])


def store(request):
    key = request.app.state.settings.artifact_encryption_key
    if not key:
        return None
    return ArtifactStore(request.app.state.db_pool, base64.b64decode(key, validate=True))


@router.post("/{job_id}/artifacts")
async def put_artifact(job_id: UUID, request: Request):
    ctx = context(request)
    if isinstance(ctx, Response):
        return ctx
    _, client = ctx
    artifacts = store(request)
    if artifacts is None:
        return JSONResponse({"error": "artifact_storage_not_configured"}, status_code=503)
    try:
        content = await request.body()
        result = await artifacts.put(client.id, job_id, content)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if result is None:
        return JSONResponse({"error": "workflow_not_found"}, status_code=404)
    return JSONResponse(result, status_code=201)


@router.get("/{job_id}/artifacts/{artifact_id}")
async def get_artifact(job_id: UUID, artifact_id: UUID, request: Request):
    ctx = context(request)
    if isinstance(ctx, Response):
        return ctx
    _, client = ctx
    artifacts = store(request)
    if artifacts is None:
        return JSONResponse({"error": "artifact_storage_not_configured"}, status_code=503)
    content = await artifacts.get(client.id, job_id, artifact_id)
    if content is None:
        return JSONResponse({"error": "artifact_not_found"}, status_code=404)
    return Response(content, media_type="application/octet-stream",
                    headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
