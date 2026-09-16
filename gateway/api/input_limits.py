"""Explicit input policy: never truncate, compact, or mutate client payloads."""

import json

from gateway.api.errors import client_error
from gateway.distributed_capacity import CapacityFull, CapacityUnavailable, DistributedCapacity
from gateway.protocols import Capability
from gateway.protocols.output_limits import output_limit
from gateway.providers import ErrorCategory
from gateway.quotas import estimate_tokens


def payload_error(normalized, registry, settings) -> str | None:
    payload = normalized.payload
    if payload.get("background") not in (None, False):
        return "Provider background execution is unsupported; use gateway workflows."
    try:
        requested_output = output_limit(normalized.protocol, payload, settings.max_output_tokens)
    except ValueError as exc:
        return str(exc)
    if type(payload.get("n", 1)) is not int or payload.get("n", 1) != 1:
        return "Only one completion per request is supported by bounded accounting."
    limits = [payload[key] for key in ("max_tokens", "max_output_tokens", "max_completion_tokens")
              if key in payload]
    for limit in limits:
        if type(limit) is not int or limit < 1 or limit > settings.max_output_tokens:
            return "Requested output token limit is invalid or exceeds the configured maximum."
    tools = payload.get("tools", [])
    if not isinstance(tools, list) or len(tools) > settings.max_tools:
        return "Tool definitions exceed the configured limit or have invalid shape."
    tool_bytes = len(json.dumps(tools, ensure_ascii=False).encode())
    media_bytes = 0
    unsupported_media = False
    pending = [(payload, 0)]
    while pending:
        value, depth = pending.pop()
        if depth > 64:
            return "Request nesting exceeds the supported limit."
        if isinstance(value, dict):
            kind = value.get("type")
            media = isinstance(kind, str) and kind in {
                "image", "image_url", "input_image", "input_audio", "audio", "video",
            }
            if kind in ("input_audio", "audio", "video"):
                unsupported_media = True
            tool_result = value.get("role") == "tool" or (
                isinstance(kind, str) and kind in {"tool_result", "function_call_output"}
            )
            if media or tool_result:
                size = len(json.dumps(value, ensure_ascii=False).encode())
                if media:
                    media_bytes += size
                else:
                    tool_bytes += size
            else:
                pending.extend((child, depth + 1) for child in value.values()
                               if isinstance(child, (dict, list)))
        elif isinstance(value, list):
            pending.extend((child, depth + 1) for child in value if isinstance(child, (dict, list)))
    if tool_bytes > settings.max_tool_payload_bytes:
        return "Tool definitions/results exceed the configured byte limit."
    if media_bytes > settings.max_multimodal_bytes:
        return "Multimodal content exceeds the configured byte limit."
    if unsupported_media:
        return "Audio/video input is not supported by the configured capability model."
    try:
        model = registry.resolve(normalized.requested_model)
    except LookupError:
        return None  # Routing retains the existing unknown/disabled-model error contract.
    if media_bytes and Capability.VISION not in model.capabilities:
        return "This model is not configured for multimodal input."
    if (model.context_window
            and estimate_tokens(payload) + requested_output > model.context_window):
        return "Estimated input plus requested output exceeds the model context window."
    return None


async def admit_authenticated(request, normalized, runtime, authenticated):
    settings = request.app.state.settings
    error = payload_error(normalized, runtime.model_registry, settings)
    if error:
        return client_error(ErrorCategory.INVALID_REQUEST, error, 400)
    admit = getattr(request.state, "admit_client", None)
    if admit and not admit(authenticated.client.id, normalized.stream):
        response = client_error(
            ErrorCategory.RATE_LIMIT, "Client or streaming capacity is full.", 429
        )
        response.headers["Retry-After"] = str(settings.overload_retry_after_seconds)
        return response
    if settings.distributed_admission_enabled:
        scopes = {
            "interactive:global": settings.distributed_max_requests,
            f"interactive:client:{authenticated.client.id}": settings.distributed_max_per_client,
        }
        if normalized.stream:
            scopes["interactive:streams"] = settings.distributed_max_streams
        manager = DistributedCapacity(request.app.state.db_pool)
        try:
            owner = await manager.acquire(scopes)
            request.state.start_distributed_admission(manager, owner, len(scopes))
        except (CapacityFull, CapacityUnavailable) as exc:
            status = 429 if isinstance(exc, CapacityFull) else 503
            response = client_error(ErrorCategory.RATE_LIMIT if status == 429
                                    else ErrorCategory.PROVIDER_UNAVAILABLE, str(exc), status)
            response.headers["Retry-After"] = str(settings.overload_retry_after_seconds)
            return response
    return None
