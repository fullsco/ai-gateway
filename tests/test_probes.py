import httpx
import pytest

from gateway.auth import ClientPermissions, GatewayClient, InMemoryGatewayKeyStore
from gateway.health.probes import run_health_probes
from gateway.models import CanonicalModel, ModelRegistry, ProviderModel
from gateway.observability import PassiveHealthRecorder
from gateway.protocols import Capability, ClientProtocol
from gateway.providers import Credential, ProviderConfig
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
