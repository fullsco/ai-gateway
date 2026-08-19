import asyncio
import json
from dataclasses import replace

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


class Limiter:
    def __init__(self, results=None):
        self.results = list(results or ["reserved:test-token"])
        self.reservations = []
        self.completions = []

    async def reserve(self, provider_id, credential_id, provider_model_id, *, manual=False):
        self.reservations.append((provider_id, credential_id, provider_model_id, manual))
        return self.results.pop(0) if self.results else "in_progress"

    async def complete(self, credential_id, reservation_token, *, success, result):
        self.completions.append((credential_id, reservation_token, success, result))

    def route_index(self, credential_id, route_count):
        return 0


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
    await run_health_probes(runtime, recorder, Limiter())
    await recorder.close()
    await runtime.http_client.aclose()

    assert seen == {"method": "GET", "content": b""}
    assert any("health_checks" in query for query, _ in pool.calls)
    health_args = next(args for query, args in pool.calls if "health_checks" in query)
    assert health_args[-1] == "automatic"


@pytest.mark.asyncio
async def test_health_probe_marks_auth_failure_without_body():
    runtime = make_runtime(
        lambda request: httpx.Response(401, json={"error": {"message": "bad"}})
    )
    pool = Pool()
    recorder = PassiveHealthRecorder(pool)
    recorder.start()
    await run_health_probes(runtime, recorder, Limiter())
    await recorder.close()
    await runtime.http_client.aclose()

    assert any(args[4] == "upstream_authentication_error" for _, args in pool.calls)
    assert all("update public.provider_credentials" not in query for query, _ in pool.calls)


@pytest.mark.asyncio
async def test_health_probe_classifies_rate_limit_as_failure():
    runtime = make_runtime(
        lambda request: httpx.Response(
            429,
            headers={"retry-after": "60"},
            json={"error": {"message": "limited"}},
        )
    )
    pool = Pool()
    recorder = PassiveHealthRecorder(pool)
    recorder.start()
    await run_health_probes(runtime, recorder, Limiter())
    await recorder.close()
    await runtime.http_client.aclose()

    assert any(args[4] == "rate_limit" for _, args in pool.calls)
    assert all("update public.provider_credentials" not in query for query, _ in pool.calls)


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
    await run_health_probes(runtime, recorder, Limiter())
    await recorder.close()
    await runtime.http_client.aclose()

    assert any(args[4] == "upstream_waf_rejection" for _, args in pool.calls)
    assert all("update public.provider_credentials" not in query for query, _ in pool.calls)


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
    await run_health_probes(runtime, None, Limiter())
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


@pytest.mark.asyncio
async def test_probe_contacts_only_one_mapping_per_credential():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200)

    runtime = make_anthropic_runtime(handler)
    route = runtime.model_registry.list_provider_models()[0]
    second = ProviderModel(
        "route-2", "model", "provider", "upstream-2",
        ClientProtocol.ANTHROPIC_MESSAGES, route.capabilities, priority=200,
    )
    registry = ModelRegistry(
        [runtime.model_registry.resolve("model")], [route, second]
    )
    adapters = {
        **runtime.provider_model_adapters,
        "route-2": runtime.provider_model_adapters["route"],
    }
    runtime = replace(
        runtime,
        model_registry=registry,
        routing_engine=RoutingEngine(registry),
        provider_model_adapters=adapters,
    )
    limiter = Limiter()

    summary = await run_health_probes(runtime, None, limiter)
    await runtime.http_client.aclose()

    assert calls == ["/v1/messages"]
    assert summary == {"contacted": 1, "skipped": 0, "credentials_without_route": 0}
    assert len(limiter.reservations) == 1


@pytest.mark.asyncio
async def test_probe_does_not_contact_provider_when_reserved_or_cooling_down():
    contacted = 0

    def handler(request):
        nonlocal contacted
        contacted += 1
        return httpx.Response(200)

    runtime = make_runtime(handler)
    limiter = Limiter(["in_progress"])
    first = await run_health_probes(runtime, None, limiter)
    limiter.results = ["cooldown"]
    second = await run_health_probes(runtime, None, limiter)
    limiter.results = ["daily_limit"]
    third = await run_health_probes(runtime, None, limiter)
    await runtime.http_client.aclose()

    assert contacted == 0
    assert [first["skipped"], second["skipped"], third["skipped"]] == [1, 1, 1]


@pytest.mark.asyncio
async def test_probe_timeout_is_one_attempt_and_applies_failure_backoff():
    attempts = 0

    def handler(request):
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("timeout", request=request)

    runtime = make_runtime(handler)
    limiter = Limiter()
    await run_health_probes(runtime, None, limiter)
    await runtime.http_client.aclose()

    assert attempts == 1
    assert limiter.completions == [
        ("credential", "test-token", False, "timeout")
    ]


@pytest.mark.asyncio
async def test_probe_failure_does_not_change_inference_route_circuit():
    runtime = make_runtime(
        lambda request: httpx.Response(503, json={"error": {"message": "down"}})
    )
    for _ in range(3):
        await run_health_probes(runtime, None, Limiter())

    assert await runtime.route_controls.allow("route") is True
    await runtime.http_client.aclose()


@pytest.mark.asyncio
async def test_manual_probe_bypasses_automatic_limits_but_uses_lease():
    contacted = 0

    def handler(request):
        nonlocal contacted
        contacted += 1
        return httpx.Response(200)

    runtime = make_runtime(handler)
    limiter = Limiter(["reserved:test-token", "in_progress"])
    first, second = await asyncio.gather(
        run_health_probes(runtime, None, limiter, manual=True),
        run_health_probes(runtime, None, limiter, manual=True),
    )
    await runtime.http_client.aclose()

    assert contacted == 1
    assert all(reservation[3] is True for reservation in limiter.reservations)
    assert sorted([first["contacted"], second["contacted"]]) == [0, 1]


@pytest.mark.asyncio
async def test_probe_respects_empty_pool_credential_allowlist():
    contacted = 0

    def handler(request):
        nonlocal contacted
        contacted += 1
        return httpx.Response(200)

    runtime = make_runtime(handler)
    route = runtime.model_registry.list_provider_models()[0]
    restricted = ProviderModel(**{**route.__dict__, "allowed_credential_ids": frozenset()})
    registry = ModelRegistry([runtime.model_registry.resolve("model")], [restricted])
    runtime = replace(runtime, model_registry=registry, routing_engine=RoutingEngine(registry))
    summary = await run_health_probes(runtime, None, Limiter())
    await runtime.http_client.aclose()

    assert contacted == 0
    assert summary["credentials_without_route"] == 1


def test_probe_rejects_a_non_api_200_response() -> None:
    """A parked or suspended domain answering 200 HTML must not read as healthy.

    Regression test: probe success used to be decided purely by status < 400, so a
    Cloudflare parking page that returns 200 on every path kept a provider that
    cannot serve a single request marked healthy.
    """
    from gateway.health.probes import _looks_like_api_response

    parked = httpx.Response(
        200,
        headers={"content-type": "text/html; charset=utf-8"},
        content=b"<html><title>Cloudflare Registrar</title></html>",
    )
    assert _looks_like_api_response(parked) is False

    real = httpx.Response(
        200, headers={"content-type": "application/json"}, json={"data": []}
    )
    assert _looks_like_api_response(real) is True

    streaming = httpx.Response(200, headers={"content-type": "text/event-stream"})
    assert _looks_like_api_response(streaming) is True
