"""Synthetic runtime/response fixtures; no configuration files or network transports."""

import asyncio
import json

import httpx

from gateway.auth import ClientPermissions, GatewayClient, InMemoryGatewayKeyStore
from gateway.models import CanonicalModel, ModelRegistry, ProviderModel
from gateway.protocols import Capability, ClientProtocol
from gateway.protocols.translate import get_translator
from gateway.providers import Credential, ProviderConfig, TranslatingAdapter
from gateway.providers.anthropic import AnthropicCompatibleAdapter
from gateway.providers.openai import OpenAICompatibleAdapter
from gateway.routing import CredentialState, ProviderState, RoutingEngine
from gateway.runtime import GatewayRuntime
from gateway.security.gateway_keys import GatewayKeyHasher

CHAT = ClientProtocol.OPENAI_CHAT_COMPLETIONS
MESSAGES = ClientProtocol.ANTHROPIC_MESSAGES
RESPONSES = ClientProtocol.OPENAI_RESPONSES
PROTOCOLS = {
    "chat_native": (CHAT, CHAT, "/v1/chat/completions"),
    "responses_native": (RESPONSES, RESPONSES, "/v1/responses"),
    "messages_native": (MESSAGES, MESSAGES, "/v1/messages"),
    "messages_via_chat": (MESSAGES, CHAT, "/v1/messages"),
}
SCENARIOS = (
    "healthy", "stream", "slow_stream", "reasoning_pause", "slow_reader", "disconnect",
    "upload_delay", "upload_timeout", "upstream_error", "oversize",
)
STREAMING = {"stream", "slow_stream", "reasoning_pause", "slow_reader", "disconnect"}


def payload(protocol: str, scenario: str, size: int) -> bytes:
    body = {"model": "synthetic", "max_tokens": 16, "stream": scenario in STREAMING}
    if PROTOCOLS[protocol][0] == RESPONSES:
        body["max_output_tokens"] = body.pop("max_tokens")
        body["input"] = ""
    else:
        body["messages"] = [{"role": "user", "content": ""}]
    encoded = json.dumps(body, separators=(",", ":")).encode()
    if size < len(encoded):
        raise ValueError("Synthetic body size cannot fit the protocol envelope")
    padding = "x" * (size - len(encoded))
    if "input" in body:
        body["input"] = padding
    else:
        body["messages"][0]["content"] = padding
    return json.dumps(body, separators=(",", ":")).encode()


def response_body(protocol):
    usage = {"input_tokens": 4, "output_tokens": 1}
    if protocol == MESSAGES:
        return {"id": "synthetic", "type": "message", "role": "assistant",
                "model": "synthetic", "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn", "usage": usage}
    if protocol == RESPONSES:
        return {"id": "synthetic", "object": "response", "status": "completed",
                "model": "synthetic", "output": [{"type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": "ok"}]}], "usage": usage}
    return {"id": "synthetic", "object": "chat.completion", "model": "synthetic",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5}}


def frames(protocol):
    if protocol == MESSAGES:
        events = [
            {"type": "message_start", "message": {**response_body(protocol), "content": []}},
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": "ok"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
             "usage": {"output_tokens": 1}}, {"type": "message_stop"},
        ]
    elif protocol == RESPONSES:
        events = [
            {"type": "response.created", "response": {"id": "synthetic"}},
            {"type": "response.output_text.delta", "delta": "ok", "output_index": 0},
            {"type": "response.completed", "response": response_body(protocol)},
        ]
    else:
        events = [
            {"id": "synthetic", "choices": [{"index": 0, "delta": delta,
             "finish_reason": finish}]}
            for delta, finish in [({"role": "assistant"}, None), ({"content": "ok"}, None),
                                  ({}, "stop")]
        ]
        events.append({"id": "synthetic", "choices": [],
                       "usage": {"prompt_tokens": 4, "completion_tokens": 1}})
    result = []
    for event in events:
        prefix = f"event: {event['type']}\n" if "type" in event else ""
        result.append((prefix + "data: " + json.dumps(event) + "\n\n").encode())
    if protocol == CHAT:
        result.append(b"data: [DONE]\n\n")
    return result


def build_runtime(protocol, scenario, current, clock, delay):
    client_protocol, upstream, _ = PROTOCOLS[protocol]

    class Stream(httpx.AsyncByteStream):
        def __init__(self, record):
            self.record = record
            self.closed = False
            record["streams_opened"] += 1

        async def __aiter__(self):
            for index, frame in enumerate(frames(upstream)):
                wait = delay if scenario in {"slow_stream", "disconnect"} else 0
                if scenario == "reasoning_pause" and index == 1:
                    wait = delay * 5
                await asyncio.sleep(wait)
                self.record.setdefault("upstream_first_byte", clock())
                yield frame

        async def aclose(self):
            if not self.closed:
                self.closed = True
                self.record["streams_closed"] += 1

    async def handler(request):
        if request.url.host != "127.0.0.1":
            raise RuntimeError("Mock upstream must be loopback")
        record = current.get()
        record["attempts"] += 1
        record.setdefault("dispatch", clock())
        # Yield even for short calls, so synchronized bursts exercise admission.
        await asyncio.sleep(delay if scenario == "healthy" else 0)
        record.setdefault("upstream_headers", clock())
        if scenario == "upstream_error":
            return httpx.Response(503, json={"error": {"message": "synthetic unavailable"}})
        if scenario in STREAMING:
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  stream=Stream(record))
        return httpx.Response(200, json=response_body(upstream))

    hasher = GatewayKeyHasher(b"b" * 32)
    issued = hasher.issue(key_id="synthetic", client_id="synthetic")
    client = GatewayClient(
        "synthetic", "Synthetic", ClientPermissions(frozenset({client_protocol}))
    )
    capabilities = frozenset({Capability.STREAMING})
    registry = ModelRegistry(
        [CanonicalModel("synthetic", frozenset(), capabilities)],
        [ProviderModel("synthetic", "synthetic", "synthetic", "synthetic", upstream,
                       capabilities, max_concurrency=128, routing_policy={"max_attempts": 1},
                       serves_protocols=frozenset({client_protocol, upstream}))],
    )
    config = ProviderConfig(id="synthetic", name="Synthetic", base_url="http://127.0.0.1",
                            protocol=upstream, capabilities=capabilities)
    adapter = (AnthropicCompatibleAdapter if upstream == MESSAGES else OpenAICompatibleAdapter)(
        config
    )
    translations = {}
    if client_protocol != upstream:
        translations[("synthetic", client_protocol)] = TranslatingAdapter(
            adapter, get_translator(client_protocol, upstream)
        )
    runtime = GatewayRuntime(
        key_store=InMemoryGatewayKeyStore([issued.record], [client]), key_hasher=hasher,
        model_registry=registry, routing_engine=RoutingEngine(registry),
        provider_states=(ProviderState("synthetic"),),
        credential_states=(CredentialState("synthetic", "synthetic"),),
        provider_model_adapters={"synthetic": adapter}, translating_adapters=translations,
        credentials={"synthetic": Credential(id="synthetic", secret="synthetic")},
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False),
    )
    return runtime, issued.plaintext
