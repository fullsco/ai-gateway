import asyncio

import httpx
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.auth import ClientPermissions, GatewayClient, InMemoryGatewayKeyStore
from gateway.config import Settings
from gateway.models import CanonicalModel, ModelRegistry, ProviderModel
from gateway.protocols import Capability, ClientProtocol
from gateway.providers import Credential, ProviderConfig
from gateway.providers.anthropic import AnthropicCompatibleAdapter
from gateway.routing import CredentialState, ProviderState, RoutingEngine
from gateway.runtime import GatewayRuntime
from gateway.security.gateway_keys import GatewayKeyHasher


class BytesStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        return None


class DelayedStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        await asyncio.sleep(1)
        yield b"never"

    async def aclose(self) -> None:
        return None


def make_runtime(handler):
    hasher = GatewayKeyHasher(b"p" * 32)
    issued = hasher.issue(key_id="key", client_id="client")
    client = GatewayClient(
        id="client",
        name="Claude Code",
        permissions=ClientPermissions(frozenset({ClientProtocol.ANTHROPIC_MESSAGES})),
    )
    capabilities = frozenset({Capability.STREAMING})
    registry = ModelRegistry(
        [CanonicalModel("model-x", frozenset(), capabilities)],
        [
            ProviderModel(
                "provider-model",
                "model-x",
                "provider-a",
                "upstream-x",
                ClientProtocol.ANTHROPIC_MESSAGES,
                capabilities,
            )
        ],
    )
    adapter = AnthropicCompatibleAdapter(
        ProviderConfig(
            id="provider-a",
            name="Provider A",
            base_url="https://upstream.example",
            protocol=ClientProtocol.ANTHROPIC_MESSAGES,
            capabilities=capabilities,
        )
    )
    runtime = GatewayRuntime(
        key_store=InMemoryGatewayKeyStore([issued.record], [client]),
        key_hasher=hasher,
        model_registry=registry,
        routing_engine=RoutingEngine(registry),
        provider_states=(ProviderState("provider-a"),),
        credential_states=(
            CredentialState("credential-a", "provider-a", priority=10),
            CredentialState("credential-b", "provider-a", priority=20),
        ),
        provider_model_adapters={"provider-model": adapter},
        credentials={
            "credential-a": Credential(id="credential-a", secret="provider-key-a"),
            "credential-b": Credential(id="credential-b", secret="provider-key-b"),
        },
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return runtime, issued.plaintext


def test_rate_limit_rotates_to_next_credential() -> None:
    used_keys = []

    def handler(request: httpx.Request):
        used_keys.append(request.headers["x-api-key"])
        if request.headers["x-api-key"] == "provider-key-a":
            return httpx.Response(429, json={"error": {"message": "rate limited"}})
        return httpx.Response(200, json={"type": "message", "id": "success"})

    runtime, key = make_runtime(handler)
    app = create_app(Settings(environment="test", _env_file=None), runtime)

    with TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            headers={"x-api-key": key},
            json={"model": "model-x", "messages": []},
        )

    assert response.status_code == 200
    assert response.json()["id"] == "success"
    assert used_keys == ["provider-key-a", "provider-key-b"]


def test_empty_stream_fails_over_before_committing_response() -> None:
    used_keys = []

    def handler(request: httpx.Request):
        used_keys.append(request.headers["x-api-key"])
        chunks = [] if request.headers["x-api-key"] == "provider-key-a" else [b"data: ok\n\n"]
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=BytesStream(chunks),
        )

    runtime, key = make_runtime(handler)
    app = create_app(Settings(environment="test", _env_file=None), runtime)

    with TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            headers={"x-api-key": key},
            json={"model": "model-x", "stream": True, "messages": []},
        )

    assert response.status_code == 200
    assert response.content == b"data: ok\n\n"
    assert used_keys == ["provider-key-a", "provider-key-b"]


def test_first_event_timeout_releases_route_and_fails_over() -> None:
    used_keys = []

    def handler(request: httpx.Request):
        used_keys.append(request.headers["x-api-key"])
        stream = (
            DelayedStream()
            if request.headers["x-api-key"] == "provider-key-a"
            else BytesStream([b"data: ok\n\n"])
        )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=stream,
        )

    runtime, key = make_runtime(handler)
    app = create_app(
        Settings(environment="test", first_event_timeout_seconds=0.01, _env_file=None),
        runtime,
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            headers={"x-api-key": key},
            json={"model": "model-x", "stream": True, "messages": []},
        )

    assert response.status_code == 200
    assert response.content == b"data: ok\n\n"
    assert used_keys == ["provider-key-a", "provider-key-b"]
