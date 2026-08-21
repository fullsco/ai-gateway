from fastapi import APIRouter, Request
from fastapi.responses import Response

from gateway.api.errors import client_error, denial_error
from gateway.api.executor import execute_request
from gateway.auth import AuthDenial, authenticate_request
from gateway.protocols import ClientProtocol, normalize_request
from gateway.providers import ErrorCategory
from gateway.runtime import GatewayRuntime

router = APIRouter()


@router.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Response:
    return await _handle_openai(request, ClientProtocol.OPENAI_CHAT_COMPLETIONS)


@router.post("/v1/responses")
async def responses(request: Request) -> Response:
    return await _handle_openai(request, ClientProtocol.OPENAI_RESPONSES)


async def _handle_openai(request: Request, protocol: ClientProtocol) -> Response:
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
        normalized = normalize_request(protocol, payload)
    except (ValueError, UnicodeDecodeError):
        return client_error(ErrorCategory.INVALID_REQUEST, "Invalid request body.", 400)

    authenticated = authenticate_request(
        request.headers,
        normalized.protocol,
        normalized.requested_model,
        store=runtime.key_store,
        hasher=runtime.key_hasher,
    )
    if isinstance(authenticated, AuthDenial):
        return denial_error(authenticated)
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
