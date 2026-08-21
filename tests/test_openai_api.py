import json

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.auth import ClientPermissions, GatewayClient, InMemoryGatewayKeyStore
from gateway.config import Settings
from gateway.models import CanonicalModel, ModelRegistry, ProviderModel
from gateway.protocols import Capability, ClientProtocol, NormalizedRequest
from gateway.providers import Credential, ProviderConfig
from gateway.providers.openai import OpenAICompatibleAdapter
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


def make_runtime(
    protocol: ClientProtocol,
    handler,
    *,
    client_protocols: frozenset[ClientProtocol] | None = None,
):
    """Build a runtime for one protocol.

    client_protocols lets a test give the client different permissions from the
    route, which is how a real misconfiguration looks: the mapping serves OpenAI
    while the calling client is only allowed Anthropic.
    """
    hasher = GatewayKeyHasher(b"p" * 32)
    issued = hasher.issue(key_id="key-1", client_id="client-1")
    client = GatewayClient(
        id="client-1",
        name="OpenAI client",
        permissions=ClientPermissions(
            client_protocols if client_protocols is not None else frozenset({protocol})
        ),
    )
    store = InMemoryGatewayKeyStore([issued.record], [client])
    capabilities = frozenset({Capability.STREAMING})
    model = CanonicalModel("model-x", frozenset({"alias-x"}), capabilities)
    provider_model = ProviderModel(
        "provider-model-x",
        "model-x",
        "provider-a",
        "upstream-x",
        protocol,
        capabilities,
    )
    registry = ModelRegistry([model], [provider_model])
    adapter = OpenAICompatibleAdapter(
        ProviderConfig(
            id="provider-a",
            name="Provider A",
            base_url="https://upstream.example",
            protocol=protocol,
            capabilities=capabilities,
        )
    )
    runtime = GatewayRuntime(
        key_store=store,
        key_hasher=hasher,
        model_registry=registry,
        routing_engine=RoutingEngine(registry),
        provider_states=(ProviderState("provider-a"),),
        credential_states=(CredentialState("credential-a", "provider-a"),),
        provider_model_adapters={"provider-model-x": adapter},
        credentials={"credential-a": Credential(id="credential-a", secret="provider-secret")},
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return runtime, issued.plaintext


@pytest.mark.parametrize(
    ("protocol", "endpoint"),
    [
        (ClientProtocol.OPENAI_CHAT_COMPLETIONS, "/v1/chat/completions"),
        (ClientProtocol.OPENAI_RESPONSES, "/v1/responses"),
    ],
)
def test_openai_endpoints_map_model_and_replace_bearer_key(protocol, endpoint) -> None:
    seen = {}

    def handler(request: httpx.Request):
        seen["path"] = request.url.path
        seen["authorization"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "response-1", "model": "upstream-x"})

    runtime, key = make_runtime(protocol, handler)
    app = create_app(Settings(environment="test", _env_file=None), runtime)

    with TestClient(app) as client:
        response = client.post(
            endpoint,
            headers={"authorization": f"Bearer {key}"},
            json={"model": "alias-x", "input": "hello"},
        )

    assert response.status_code == 200
    assert seen["path"] == endpoint
    assert seen["authorization"] == "Bearer provider-secret"
    assert seen["body"]["model"] == "upstream-x"


def test_openai_stream_is_relayed() -> None:
    runtime, key = make_runtime(
        ClientProtocol.OPENAI_RESPONSES,
        lambda _: httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=BytesStream([b'data: {"type":"response.completed"}\n\n']),
        ),
    )
    app = create_app(Settings(environment="test", _env_file=None), runtime)

    with TestClient(app) as client:
        response = client.post(
            "/v1/responses",
            headers={"authorization": f"Bearer {key}"},
            json={"model": "alias-x", "input": "hello", "stream": True},
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.content == b'data: {"type":"response.completed"}\n\n'


def test_openai_adapter_applies_default_headers() -> None:
    adapter = OpenAICompatibleAdapter(
        ProviderConfig(
            id="provider-a",
            name="Provider A",
            base_url="https://upstream.example",
            protocol=ClientProtocol.OPENAI_RESPONSES,
            capabilities=frozenset(),
        ),
        default_headers={"user-agent": "codex_cli_rs/test", "originator": "codex_cli_rs"},
    )
    request = adapter.create_request(
        NormalizedRequest(
            protocol=ClientProtocol.OPENAI_RESPONSES,
            requested_model="model-x",
            payload={"model": "model-x"},
        ),
        Credential(id="credential-a", secret="provider-secret"),
    )

    assert request.headers["user-agent"] == "codex_cli_rs/test"
    assert request.headers["originator"] == "codex_cli_rs"


def test_a_key_without_the_protocol_is_told_that_and_not_that_it_is_invalid() -> None:
    """A permission gap must not be reported as a bad secret.

    In production OpenCode was configured with a key whose client only permitted
    anthropic_messages. Every gateway-openai request failed with 401 "Invalid
    gateway key", so the key was doubted, rotated and re-checked while the actual
    cause was one missing entry in the client's allowed protocols.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("upstream must not be called when the client is denied")

    # The route serves OpenAI, but the calling client may only use Anthropic.
    runtime, key = make_runtime(
        ClientProtocol.OPENAI_CHAT_COMPLETIONS,
        handler,
        client_protocols=frozenset({ClientProtocol.ANTHROPIC_MESSAGES}),
    )
    app = create_app(Settings(environment="test", _env_file=None), runtime)

    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"authorization": f"Bearer {key}"},
            json={"model": "model-x", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert response.status_code == 403
    body = response.json()["error"]
    assert body["type"] == "authorization_error"
    assert "not permitted to use this API" in body["message"]
    assert "Invalid gateway key" not in body["message"]


def test_an_unknown_key_is_still_reported_as_invalid() -> None:
    """The opposite case must not regress into a permission message."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("upstream must not be called for an unknown key")

    runtime, _ = make_runtime(ClientProtocol.OPENAI_CHAT_COMPLETIONS, handler)
    app = create_app(Settings(environment="test", _env_file=None), runtime)

    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"authorization": "Bearer gw_live_notarealkey_0000000000"},
            json={"model": "model-x", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert response.status_code == 401
    body = response.json()["error"]
    assert body["type"] == "authentication_error"
    assert body["message"] == "Invalid gateway key."
