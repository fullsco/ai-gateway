from fastapi import APIRouter, Request
from fastapi.responses import Response

from gateway.api.errors import client_error
from gateway.api.executor import execute_request
from gateway.auth import authenticate_request
from gateway.protocols import ClientProtocol, normalize_request
from gateway.providers import ErrorCategory
from gateway.runtime import GatewayRuntime

router = APIRouter()


@router.post("/v1/messages")
async def messages(request: Request) -> Response:
    runtime: GatewayRuntime | None = getattr(request.app.state, "runtime", None)
    if runtime is None:
        return client_error(
            ErrorCategory.PROVIDER_UNAVAILABLE,
            "The gateway has no active runtime configuration.",
            503,
        )
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("Request body must be a JSON object")
        normalized = normalize_request(ClientProtocol.ANTHROPIC_MESSAGES, payload)
    except (ValueError, UnicodeDecodeError):
        return client_error(ErrorCategory.INVALID_REQUEST, "Invalid request body.", 400)

    authenticated = authenticate_request(
        request.headers,
        normalized.protocol,
        normalized.requested_model,
        store=runtime.key_store,
        hasher=runtime.key_hasher,
    )
    if authenticated is None:
        return client_error(ErrorCategory.AUTHENTICATION_ERROR, "Invalid gateway key.", 401)
    return await execute_request(
        normalized,
        request.headers,
        runtime,
        request.app.state.settings,
        client_id=authenticated.client.id,
        key_id=authenticated.key_id,
        db_pool=getattr(request.app.state, "db_pool", None),
        health_recorder=getattr(request.app.state, "health_recorder", None),
        live_state=getattr(request.app.state, "live_state", None),
    )
