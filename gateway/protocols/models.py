from copy import deepcopy
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ClientProtocol(StrEnum):
    ANTHROPIC_MESSAGES = "anthropic_messages"
    OPENAI_CHAT_COMPLETIONS = "openai_chat_completions"
    OPENAI_RESPONSES = "openai_responses"


class Capability(StrEnum):
    STREAMING = "streaming"
    TOOL_CALLING = "tool_calling"
    VISION = "vision"
    REASONING = "reasoning"
    STRUCTURED_OUTPUT = "structured_output"
    COMPUTER_USE = "computer_use"


class NormalizedRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    protocol: ClientProtocol
    requested_model: str = Field(min_length=1)
    stream: bool = False
    required_capabilities: frozenset[Capability] = frozenset()
    payload: dict[str, Any]


def normalize_request(
    protocol: ClientProtocol,
    payload: dict[str, Any],
) -> NormalizedRequest:
    model = payload.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("Request model must be a non-empty string")

    capabilities: set[Capability] = set()
    if payload.get("stream") is True:
        capabilities.add(Capability.STREAMING)
    if payload.get("tools"):
        capabilities.add(Capability.TOOL_CALLING)
    if payload.get("thinking") or payload.get("reasoning"):
        capabilities.add(Capability.REASONING)
    if payload.get("response_format"):
        capabilities.add(Capability.STRUCTURED_OUTPUT)

    return NormalizedRequest(
        protocol=protocol,
        requested_model=model.strip(),
        stream=payload.get("stream") is True,
        required_capabilities=frozenset(capabilities),
        payload=deepcopy(payload),
    )
