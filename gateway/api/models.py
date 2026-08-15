from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from gateway.api.errors import client_error
from gateway.auth import authenticate_key
from gateway.providers import ErrorCategory
from gateway.runtime import GatewayRuntime

router = APIRouter()


@router.get("/v1/models")
async def models(request: Request) -> JSONResponse:
    runtime: GatewayRuntime | None = getattr(request.app.state, "runtime", None)
    if runtime is None:
        return client_error(
            ErrorCategory.PROVIDER_UNAVAILABLE,
            "The gateway has no active runtime configuration.",
            503,
        )
    authenticated = authenticate_key(
        request.headers,
        store=runtime.key_store,
        hasher=runtime.key_hasher,
    )
    if authenticated is None:
        return client_error(ErrorCategory.AUTHENTICATION_ERROR, "Invalid gateway key.", 401)
    allowed = authenticated.client.permissions.allowed_models
    data = [
        {"id": model.id, "object": "model", "owned_by": "gateway"}
        for model in runtime.model_registry.list_enabled()
        if not allowed or model.id in allowed or bool(model.aliases & allowed)
    ]
    return JSONResponse({"object": "list", "data": data})
