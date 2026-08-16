import json

import httpx
import pytest

from gateway.auth import ClientPermissions, GatewayClient, InMemoryGatewayKeyStore
from gateway.health.probes import run_health_probes
from gateway.models import CanonicalModel, ModelRegistry, ProviderModel
from gateway.observability import PassiveHealthRecorder
from gateway.protocols import Capability, ClientProtocol
from gateway.providers import Credential, ProviderConfig
from gateway.providers.anthropic import AnthropicCompatibleAdapter
from gateway.providers.openai import OpenAICompatibleAdapter
from gateway.routing import CredentialState, ProviderState, RouteControls, RoutingEngine
from gateway.runtime import GatewayRuntime
from gateway.security import GatewayKeyHasher


class Pool:
    def __init__(self):
        self.calls = []

    async def execute(self, query, *args):
        self.calls.append((query, args))

    async def fetchval(self, query, *args):
        return 1


def make_runtime(handler):
    hasher = GatewayKeyHasher(b"p" * 32)
    issued = hasher.issue(key_id="key", client_id="client")
    client = GatewayClient(
        "client", "test", ClientPermissions(frozenset({ClientProtocol.OPENAI_RESPONSES}))
    )
    caps = frozenset({Capability.STREAMING})
    model = CanonicalModel("model", frozenset(), caps)
    route = ProviderModel(
        "route", "model", "provider", "upstream", ClientProtocol.OPENAI_RESPONSES, caps
    )
    registry = ModelRegistry([model], [route])
    adapter = OpenAICompatibleAdapter(
        ProviderConfig(
            id="provider",
            name="Provider",
            base_url="https://upstream.example",
            protocol=ClientProtocol.OPENAI_RESPONSES,
            capabilities=caps,
        )
    )
    return GatewayRuntime(
        key_store=InMemoryGatewayKeyStore([issued.record], [client]),
        key_hasher=hasher,
        model_registry=registry,
        routing_engine=RoutingEngine(registry),
        provider_states=(ProviderState("provider"),),
        credential_states=(CredentialState("credential", "provider"),),
        provider_model_adapters={"route": adapter},
        credentials={"credential": Credential(id="credential", secret="secret")},
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        route_controls=RouteControls(),
    )


def make_anthropic_runtime(handler):
    runtime = make_runtime(handler)
    adapter = AnthropicCompatibleAdapter(
        ProviderConfig(
            id="provider",
            name="Provider",
            base_url="https://upstream.example",
            protocol=ClientProtocol.ANTHROPIC_MESSAGES,
            capabilities=frozenset({Capability.STREAMING}),
        )
    )
    route = ProviderModel(
        "route", "model", "provider", "upstream", ClientProtocol.ANTHROPIC_MESSAGES,
        frozenset({Capability.STREAMING}),
    )
    model = CanonicalModel("model", frozenset(), frozenset({Capability.STREAMING}))
    registry = ModelRegistry([model], [route])
    return runtime.__class__(
        **{
            **runtime.__dict__,
            "model_registry": registry,
            "routing_engine": RoutingEngine(registry),
            "provider_model_adapters": {"route": adapter},
            "http_client": httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        }
    )


@pytest.mark.asyncio
async def test_health_probe_is_payload_free_and_records_success():
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["content"] = request.content
        return httpx.Response(200)

    runtime = make_runtime(handler)
    pool = Pool()
    recorder = PassiveHealthRecorder(pool)
    recorder.start()
    await run_health_probes(runtime, recorder)
    await recorder.close()
    await runtime.http_client.aclose()

    assert seen == {"method": "HEAD", "content": b""}
    assert any("health_checks" in query for query, _ in pool.calls)


@pytest.mark.asyncio
async def test_health_probe_marks_auth_failure_without_body():
    runtime = make_runtime(
        lambda request: httpx.Response(401, json={"error": {"message": "bad"}})
    )
    pool = Pool()
    recorder = PassiveHealthRecorder(pool)
    recorder.start()
    await run_health_probes(runtime, recorder)
    await recorder.close()
    await runtime.http_client.aclose()

    assert any(args[2] == "upstream_authentication_error" for _, args in pool.calls)


@pytest.mark.asyncio
async def test_anthropic_probe_403_does_not_poison_credential_health():
    runtime = make_anthropic_runtime(
        lambda request: httpx.Response(
            403,
            json={"error": {"type": "forbidden", "message": "challenge"}},
        )
    )
    pool = Pool()
    recorder = PassiveHealthRecorder(pool)
    recorder.start()
    await run_health_probes(runtime, recorder)
    await recorder.close()
    await runtime.http_client.aclose()

    assert any(args[2] == "upstream_waf_rejection" for _, args in pool.calls)


@pytest.mark.asyncio
async def test_anthropic_health_probe_uses_messages_contract():
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200)

    runtime = make_anthropic_runtime(handler)
    await run_health_probes(runtime, None)
    await runtime.http_client.aclose()

    assert seen["method"] == "POST"
    assert seen["path"] == "/v1/messages"
    assert seen["headers"]["user-agent"] == "ai-gateway/0.1"
    assert seen["headers"]["anthropic-version"] == "2023-06-01"
    assert seen["headers"]["x-api-key"] == "secret"
    assert seen["body"] == {
        "model": "upstream",
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "ping"}],
    }
