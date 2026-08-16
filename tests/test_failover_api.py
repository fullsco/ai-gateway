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


def make_runtime(handler, *, allow_model_fallback: bool = False, alternate_route: bool = False):
    hasher = GatewayKeyHasher(b"p" * 32)
    issued = hasher.issue(key_id="key", client_id="client")
    client = GatewayClient(
        id="client",
        name="Claude Code",
        permissions=ClientPermissions(frozenset({ClientProtocol.ANTHROPIC_MESSAGES})),
    )
    capabilities = frozenset({Capability.STREAMING})
    provider_models = [
        ProviderModel(
            "provider-model",
            "model-x",
            "provider-a",
            "upstream-x",
            ClientProtocol.ANTHROPIC_MESSAGES,
            capabilities,
            priority=10,
            allow_model_fallback=allow_model_fallback,
        )
    ]
    if alternate_route:
        provider_models.append(
            ProviderModel(
                "provider-model-b",
                "model-x",
                "provider-b",
                "upstream-b",
                ClientProtocol.ANTHROPIC_MESSAGES,
                capabilities,
                priority=20,
            )
        )
    registry = ModelRegistry(
        [CanonicalModel("model-x", frozenset(), capabilities)],
        provider_models,
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
    adapters = {"provider-model": adapter}
    if alternate_route:
        adapters["provider-model-b"] = AnthropicCompatibleAdapter(
            ProviderConfig(
                id="provider-b",
                name="Provider B",
                base_url="https://fallback.example",
                protocol=ClientProtocol.ANTHROPIC_MESSAGES,
                capabilities=capabilities,
            )
        )
    runtime = GatewayRuntime(
        key_store=InMemoryGatewayKeyStore([issued.record], [client]),
        key_hasher=hasher,
        model_registry=registry,
        routing_engine=RoutingEngine(registry),
        provider_states=(ProviderState("provider-a"), ProviderState("provider-b")),
        credential_states=(
            CredentialState("credential-a", "provider-a", priority=10),
            CredentialState("credential-b", "provider-a", priority=20),
            CredentialState("credential-c", "provider-b", priority=10),
        ),
        provider_model_adapters=adapters,
        credentials={
            "credential-a": Credential(id="credential-a", secret="provider-key-a"),
            "credential-b": Credential(id="credential-b", secret="provider-key-b"),
            "credential-c": Credential(id="credential-c", secret="provider-key-c"),
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


def test_failed_route_does_not_fallback_to_another_mapping_without_permission() -> None:
    used_hosts = []

    def handler(request: httpx.Request):
        used_hosts.append(request.url.host)
        return httpx.Response(503, json={"error": {"message": "unavailable"}})

    runtime, key = make_runtime(handler, alternate_route=True)
    app = create_app(Settings(environment="test", _env_file=None), runtime)

    with TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            headers={"x-api-key": key},
            json={"model": "model-x", "messages": []},
        )

    assert response.status_code == 503
    assert used_hosts == ["upstream.example", "upstream.example"]


def test_failed_route_falls_back_to_another_mapping_when_permitted() -> None:
    used_hosts = []

    def handler(request: httpx.Request):
        used_hosts.append(request.url.host)
        if request.url.host == "upstream.example":
            return httpx.Response(503, json={"error": {"message": "unavailable"}})
        return httpx.Response(200, json={"id": "fallback-success"})

    runtime, key = make_runtime(
        handler, allow_model_fallback=True, alternate_route=True
    )
    app = create_app(Settings(environment="test", _env_file=None), runtime)

    with TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            headers={"x-api-key": key},
            json={"model": "model-x", "messages": []},
        )

    assert response.status_code == 200
    assert used_hosts == ["upstream.example", "upstream.example", "fallback.example"]
