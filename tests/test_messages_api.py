import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.auth import ClientPermissions, GatewayClient, InMemoryGatewayKeyStore
from gateway.config import Settings
from gateway.models import CanonicalModel, ModelRegistry, ProviderModel
from gateway.protocols import Capability, ClientProtocol, NormalizedRequest
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


def make_runtime(handler):
    hasher = GatewayKeyHasher(b"p" * 32)
    issued = hasher.issue(key_id="key-1", client_id="client-1")
    client = GatewayClient(
        id="client-1",
        name="test",
        permissions=ClientPermissions(frozenset({ClientProtocol.ANTHROPIC_MESSAGES})),
    )
    store = InMemoryGatewayKeyStore([issued.record], [client])
    capabilities = frozenset({Capability.STREAMING})
    model = CanonicalModel("model-x", frozenset({"alias-x"}), capabilities)
    provider_model = ProviderModel(
        "provider-model-x",
        "model-x",
        "provider-a",
        "upstream-x",
        ClientProtocol.ANTHROPIC_MESSAGES,
        capabilities,
    )
    registry = ModelRegistry([model], [provider_model])
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
        key_store=store,
        key_hasher=hasher,
        model_registry=registry,
        routing_engine=RoutingEngine(registry),
        provider_states=(ProviderState("provider-a"),),
        credential_states=(CredentialState("credential-a", "provider-a"),),
        provider_model_adapters={"provider-model-x": adapter},
        credentials={"credential-a": Credential(id="credential-a", secret="upstream-key")},
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return runtime, issued.plaintext


def make_pool_runtime(
    handler,
    *,
    credential_ids,
    upstream_model="upstream-x",
    priorities=None,
    max_attempts=None,
):
    """One provider, one route, N credentials - the shape AgentRouter has.

    `handler` receives the outgoing request; a credential-aware handler can
    distinguish keys by the x-api-key header each credential sends. `priorities`
    maps credential id -> member priority (lower is preferred) so a test can make
    selection deterministic instead of relying on the tie-breaking RNG.
    """
    hasher = GatewayKeyHasher(b"p" * 32)
    issued = hasher.issue(key_id="key-1", client_id="client-1")
    client = GatewayClient(
        id="client-1",
        name="test",
        permissions=ClientPermissions(frozenset({ClientProtocol.ANTHROPIC_MESSAGES})),
    )
    store = InMemoryGatewayKeyStore([issued.record], [client])
    capabilities = frozenset({Capability.STREAMING})
    model = CanonicalModel("model-x", frozenset({"alias-x"}), capabilities)
    provider_model = ProviderModel(
        "provider-model-x",
        "model-x",
        "provider-a",
        upstream_model,
        ClientProtocol.ANTHROPIC_MESSAGES,
        capabilities,
    )
    overrides = {}
    if priorities:
        overrides["pool_members"] = {
            cred: {"priority": priorities[cred], "weight": 1} for cred in credential_ids
        }
    if max_attempts:
        overrides["routing_policy"] = {"max_attempts": max_attempts}
    if overrides:
        provider_model = replace(provider_model, **overrides)
    registry = ModelRegistry([model], [provider_model])
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
        key_store=store,
        key_hasher=hasher,
        model_registry=registry,
        routing_engine=RoutingEngine(registry),
        provider_states=(ProviderState("provider-a"),),
        credential_states=tuple(
            CredentialState(cred, "provider-a") for cred in credential_ids
        ),
        provider_model_adapters={"provider-model-x": adapter},
        credentials={
            cred: Credential(id=cred, secret=f"secret-{cred}") for cred in credential_ids
        },
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return runtime, issued.plaintext


def make_family_runtime(
    handler,
    *,
    credential_ids,
    upstream_by_alias,
    priorities=None,
    max_attempts=None,
):
    """One provider, two routes (two families) over the same credentials - the
    shape that proves a budget-pool stock-out on one family leaves the other
    served. `upstream_by_alias` maps alias -> upstream model id. `priorities`
    maps credential id -> member priority, applied to every route.
    """
    hasher = GatewayKeyHasher(b"p" * 32)
    issued = hasher.issue(key_id="key-1", client_id="client-1")
    client = GatewayClient(
        id="client-1",
        name="test",
        permissions=ClientPermissions(frozenset({ClientProtocol.ANTHROPIC_MESSAGES})),
    )
    store = InMemoryGatewayKeyStore([issued.record], [client])
    capabilities = frozenset({Capability.STREAMING})
    aliases = list(upstream_by_alias)
    models = [
        CanonicalModel(f"model-{alias}", frozenset({alias}), capabilities)
        for alias in aliases
    ]
    provider_models = [
        ProviderModel(
            f"provider-model-{alias}",
            f"model-{alias}",
            "provider-a",
            upstream,
            ClientProtocol.ANTHROPIC_MESSAGES,
            capabilities,
        )
        for alias, upstream in upstream_by_alias.items()
    ]
    registry = ModelRegistry(models, provider_models)
    adapter = AnthropicCompatibleAdapter(
        ProviderConfig(
            id="provider-a",
            name="Provider A",
            base_url="https://upstream.example",
            protocol=ClientProtocol.ANTHROPIC_MESSAGES,
            capabilities=capabilities,
        )
    )
    overrides = {}
    if priorities:
        overrides["pool_members"] = {
            cred: {"priority": priorities[cred], "weight": 1} for cred in credential_ids
        }
    if max_attempts:
        overrides["routing_policy"] = {"max_attempts": max_attempts}
    if overrides:
        provider_models = [replace(pm, **overrides) for pm in provider_models]
    registry = ModelRegistry(models, provider_models)
    runtime = GatewayRuntime(
        key_store=store,
        key_hasher=hasher,
        model_registry=registry,
        routing_engine=RoutingEngine(registry),
        provider_states=(ProviderState("provider-a"),),
        credential_states=tuple(
            CredentialState(cred, "provider-a") for cred in credential_ids
        ),
        provider_model_adapters={
            f"provider-model-{alias}": adapter for alias in upstream_by_alias
        },
        credentials={
            cred: Credential(id=cred, secret=f"secret-{cred}") for cred in credential_ids
        },
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return runtime, issued.plaintext


BUDGET_POOL_402 = {
    "error": {
        "type": "bad_response_status_code",
        "message": (
            "Budget pool quota has been exhausted. Please ask an administrator "
            "to increase the limit or select another budget pool."
        ),
    },
    "type": "error",
}


def test_messages_requires_gateway_key() -> None:
    runtime, _ = make_runtime(lambda _: httpx.Response(200, json={"ok": True}))
    app = create_app(Settings(environment="test", _env_file=None), runtime)

    with TestClient(app) as client:
        response = client.post("/v1/messages", json={"model": "alias-x", "messages": []})

    assert response.status_code == 401
    assert response.json()["error"]["type"] == "authentication_error"


def test_non_streaming_request_maps_model_and_replaces_client_auth() -> None:
    seen = {}

    def handler(request: httpx.Request):
        seen["body"] = request.content
        seen["api_key"] = request.headers["x-api-key"]
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json={"type": "message", "model": "upstream-x"})

    runtime, key = make_runtime(handler)
    app = create_app(Settings(environment="test", _env_file=None), runtime)

    with TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            headers={"x-api-key": key, "authorization": "Bearer client-secret"},
            json={"model": "alias-x", "messages": []},
        )

    assert response.status_code == 200
    assert b'"model":"upstream-x"' in seen["body"]
    assert seen["api_key"] == "upstream-key"
    assert seen["authorization"] is None


def test_anthropic_adapter_supports_bearer_auth_and_endpoint_query() -> None:
    adapter = AnthropicCompatibleAdapter(
        ProviderConfig(
            id="provider-a",
            name="Provider A",
            base_url="https://upstream.example",
            protocol=ClientProtocol.ANTHROPIC_MESSAGES,
            capabilities=frozenset(),
        ),
        default_headers={"user-agent": "claude-cli/test"},
        auth_scheme="bearer",
        endpoint_query={"beta": "true"},
    )
    request = adapter.create_request(
        NormalizedRequest(
            protocol=ClientProtocol.ANTHROPIC_MESSAGES,
            requested_model="model-x",
            payload={"model": "model-x"},
        ),
        Credential(id="credential-a", secret="upstream-key"),
    )

    assert request.url == "https://upstream.example/v1/messages?beta=true"
    assert request.headers["authorization"] == "Bearer upstream-key"
    assert "x-api-key" not in request.headers
    assert request.headers["user-agent"] == "claude-cli/test"


def test_streaming_response_is_relayed_without_full_buffering() -> None:
    runtime, key = make_runtime(
        lambda _: httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=BytesStream([b"event: done\ndata: {}\n\n"]),
        )
    )
    app = create_app(Settings(environment="test", _env_file=None), runtime)

    with TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            headers={"x-api-key": key},
            json={"model": "alias-x", "stream": True, "messages": []},
        )

    assert response.status_code == 200
    assert response.content == b"event: done\ndata: {}\n\n"


def test_labeled_stream_accepts_a_split_first_event_field() -> None:
    runtime, key = make_runtime(
        lambda _: httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=BytesStream([b"eve", b"nt: done\ndata: {}\n\n"]),
        )
    )
    app = create_app(Settings(environment="test", _env_file=None), runtime)

    with TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            headers={"x-api-key": key},
            json={"model": "alias-x", "stream": True, "messages": []},
        )

    assert response.status_code == 200
    assert response.content == b"event: done\ndata: {}\n\n"


def test_retryable_error_returns_normalized_error_after_routes_exhausted() -> None:
    runtime, key = make_runtime(
        lambda _: httpx.Response(503, json={"error": {"message": "provider down"}})
    )
    app = create_app(Settings(environment="test", _env_file=None), runtime)

    with TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            headers={"x-api-key": key},
            json={"model": "alias-x", "messages": []},
        )

    assert response.status_code == 503
    assert response.json()["error"]["type"] == "provider_unavailable"
    assert response.headers["x-request-id"].startswith("gw_")


def test_html_upstream_response_is_not_returned_as_success() -> None:
    runtime, key = make_runtime(
        lambda _: httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b"<html>challenge</html>",
        )
    )
    app = create_app(Settings(environment="test", _env_file=None), runtime)

    with TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            headers={"x-api-key": key},
            json={"model": "alias-x", "messages": []},
        )

    assert response.status_code == 502
    assert response.json()["error"]["type"] == "upstream_waf_rejection"


def test_valid_json_with_text_plain_content_type_is_returned() -> None:
    runtime, key = make_runtime(
        lambda _: httpx.Response(
            200,
            headers={"content-type": "text/plain; charset=utf-8"},
            json={"type": "message", "id": "success"},
        )
    )
    app = create_app(Settings(environment="test", _env_file=None), runtime)

    with TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            headers={"x-api-key": key},
            json={"model": "alias-x", "messages": []},
        )

    assert response.status_code == 200
    assert response.json()["id"] == "success"


def test_html_stream_is_not_committed_as_a_successful_stream() -> None:
    runtime, key = make_runtime(
        lambda _: httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b"<html>challenge</html>",
        )
    )
    app = create_app(Settings(environment="test", _env_file=None), runtime)

    with TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            headers={"x-api-key": key},
            json={"model": "alias-x", "stream": True, "messages": []},
        )

    assert response.status_code == 502
    assert response.json()["error"]["type"] == "upstream_waf_rejection"


def test_transport_failure_logs_safe_exception_metadata() -> None:
    def handler(request: httpx.Request):
        raise httpx.ConnectError("sensitive transport detail", request=request)

    runtime, key = make_runtime(handler)
    app = create_app(Settings(environment="test", _env_file=None), runtime)

    with patch("gateway.api.executor.log_event") as emit, TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            headers={"x-api-key": key},
            json={"model": "alias-x", "messages": []},
        )

    assert response.status_code == 503
    event = next(
        call for call in emit.call_args_list if call.args[2] == "upstream_transport_failed"
    )
    assert event.kwargs["provider_id"] == "provider-a"
    assert event.kwargs["provider_model_id"] == "provider-model-x"
    assert event.kwargs["attempt_number"] == 1
    assert event.kwargs["duration_ms"] >= 0
    assert event.kwargs["transport_error_type"] == "ConnectError"
    assert event.kwargs["transport_cause_type"] is None
    assert "sensitive transport detail" not in str(event)


def test_repeated_transport_failures_open_live_route_circuit() -> None:
    calls = 0

    def handler(request: httpx.Request):
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("provider unavailable", request=request)

    runtime, key = make_runtime(handler)
    app = create_app(Settings(environment="test", _env_file=None), runtime)

    with TestClient(app) as client:
        responses = [
            client.post(
                "/v1/messages",
                headers={"x-api-key": key},
                json={"model": "alias-x", "messages": []},
            )
            for _ in range(4)
        ]

    assert [response.status_code for response in responses] == [503, 503, 503, 503]
    assert calls == 3


def test_a_rationed_out_credential_is_skipped_not_the_whole_route() -> None:
    """A budget-pool stock-out is per credential, so it must not open the route.

    AgentRouter rations the Claude/GPT family into batches; when a key's batch runs
    out it answers 402 "Budget pool quota has been exhausted". Measured 2026-09-13
    18:54 UTC: the drain is per credential - six keys answered 200 on the rationed
    model while fourteen answered 402 at the same moment. An earlier fix treated
    the drain as route-scoped and let three 402s open the route circuit, which
    refused service from the six keys that were still serving (measured 17:33:
    four requests failed as no_eligible_route in ~200ms). The route circuit must
    stay closed: a sibling key may still have allotment, so every request still
    spends its attempts, and the per-(credential, route) stock-out memory in live
    state - not the circuit - is what skips drained keys.
    """
    calls = 0

    def handler(request: httpx.Request):
        nonlocal calls
        calls += 1
        return httpx.Response(
            402,
            json={
                "error": {
                    "type": "bad_response_status_code",
                    "message": (
                        "Budget pool quota has been exhausted. Please ask an "
                        "administrator to increase the limit or select another "
                        "budget pool."
                    ),
                },
                "type": "error",
            },
        )

    runtime, key = make_runtime(handler)
    app = create_app(Settings(environment="test", _env_file=None), runtime)

    with TestClient(app) as client:
        responses = [
            client.post(
                "/v1/messages",
                headers={"x-api-key": key},
                json={"model": "alias-x", "messages": []},
            )
            for _ in range(4)
        ]

    # The single credential 402s once per request and every response reports the
    # truthful budget_pool_stockout reason instead of the old 404 "provider does
    # not serve it". The route circuit never opens - the stock-out is remembered
    # per (credential, route) in live state instead, so the next request is
    # refused in selection (zero upstream calls) with the same truthful reason
    # rather than by a 30s circuit that would also refuse the six keys measured
    # still serving.
    assert [response.status_code for response in responses] == [503, 503, 503, 503]
    assert [r.json()["error"]["type"] for r in responses] == [
        "budget_pool_stockout",
        "budget_pool_stockout",
        "budget_pool_stockout",
        "budget_pool_stockout",
    ]
    # One upstream call total: the first request observed the stock-out; the
    # later three were refused by the per-credential memory, not by a circuit.
    assert calls == 1


def _sent_key(request: httpx.Request) -> str:
    """Credential id of the outgoing request: the upstream secret is secret-<id>."""
    secret = request.headers.get("x-api-key", "")
    return secret.removeprefix("secret-")


def test_drained_credential_is_skipped_for_the_affected_route() -> None:
    """One key drained of the rationed family must not be picked again on it.

    Measured 2026-09-13: fourteen of twenty AgentRouter keys answered 402 budget
    pool on gpt-6-astra while six answered 200. With the drained key remembered
    per route, the next request goes straight to a healthy sibling instead of
    gambling the attempt budget on keys already known to be drained.
    """
    stockout_keys = {"drained"}
    calls: list[str] = []

    def handler(request: httpx.Request):
        key = _sent_key(request)
        calls.append(key)
        if key in stockout_keys:
            return httpx.Response(402, json=BUDGET_POOL_402)
        return httpx.Response(200, json={"type": "message", "model": "upstream-x"})

    # The drained key is the preferred one, so without the stock-out memory every
    # request would spend its first attempt on it.
    runtime, key = make_pool_runtime(
        handler,
        credential_ids=["drained", "healthy"],
        priorities={"drained": 10, "healthy": 20},
    )
    app = create_app(Settings(environment="test", _env_file=None), runtime)

    with TestClient(app) as client:
        first = client.post(
            "/v1/messages",
            headers={"x-api-key": key},
            json={"model": "alias-x", "messages": []},
        )
        assert first.status_code == 200  # failed over from drained to healthy
        first_calls = len(calls)

        second = client.post(
            "/v1/messages",
            headers={"x-api-key": key},
            json={"model": "alias-x", "messages": []},
        )
        assert second.status_code == 200

    # The second request never revisits the drained key: the live-state memory
    # excludes it for this route, so only the healthy key is spent.
    assert calls[first_calls:] == ["healthy"]


def test_drained_credential_still_serves_unaffected_families() -> None:
    """The drain is per family: glm must keep flowing on the same key.

    Measured 2026-09-13 18:54 UTC: the fourteen keys that 402'd on the Claude/GPT
    ration all answered 200 on glm-5.3. Excluding the credential from every route
    because one family drained would strand the healthy ones - exactly what the
    original quota misclassification did on 2026-09-12.
    """
    rationed_keys = {"drained"}
    glm_keys: list[str] = []

    def handler(request: httpx.Request):
        key = _sent_key(request)
        body = json.loads(request.content)
        if body["model"] == "upstream-glm":
            glm_keys.append(key)
            return httpx.Response(200, json={"type": "message", "model": "upstream-glm"})
        if key in rationed_keys:
            return httpx.Response(402, json=BUDGET_POOL_402)
        return httpx.Response(200, json={"type": "message", "model": "upstream-astra"})

    runtime, key = make_family_runtime(
        handler,
        credential_ids=["drained", "healthy"],
        upstream_by_alias={"alias-astra": "upstream-astra", "alias-glm": "upstream-glm"},
        priorities={"drained": 10, "healthy": 20},
    )
    app = create_app(Settings(environment="test", _env_file=None), runtime)

    with TestClient(app) as client:
        # Drain the preferred key on the rationed family; the sibling answers.
        astra = client.post(
            "/v1/messages",
            headers={"x-api-key": key},
            json={"model": "alias-astra", "messages": []},
        )
        assert astra.status_code == 200

        # The unaffected family on the same credentials: the drained key is still
        # preferred there and must be allowed to answer.
        glm = client.post(
            "/v1/messages",
            headers={"x-api-key": key},
            json={"model": "alias-glm", "messages": []},
        )

    assert glm.status_code == 200
    assert glm_keys == ["drained"]


def test_one_credentials_stockout_cannot_open_the_whole_route() -> None:
    """One key's 402 must not refuse service from keys still serving.

    The Sep-12 fix fed budget-pool 402s into the route circuit; three of them
    (from drained keys) opened the route for everyone. Measured 17:33 on
    2026-09-13: six keys were live on astra, three drained keys were tried first,
    and four client requests failed as no_eligible_route in ~200ms.
    """
    rationed_keys = {"k1", "k2", "k3"}
    calls: list[str] = []

    def handler(request: httpx.Request):
        key = _sent_key(request)
        calls.append(key)
        if key in rationed_keys:
            return httpx.Response(402, json=BUDGET_POOL_402)
        return httpx.Response(200, json={"type": "message", "model": "upstream-x"})

    # The three drained keys are all preferred over the live one, so every
    # request's attempt budget is spent on them first unless the memory holds.
    runtime, key = make_pool_runtime(
        handler,
        credential_ids=["k1", "k2", "k3", "live"],
        priorities={"k1": 10, "k2": 11, "k3": 12, "live": 20},
        max_attempts=4,
    )
    app = create_app(Settings(environment="test", _env_file=None), runtime)

    responses = []
    with TestClient(app) as client:
        for _ in range(4):
            responses.append(
                client.post(
                    "/v1/messages",
                    headers={"x-api-key": key},
                    json={"model": "alias-x", "messages": []},
                )
            )

    # Every request succeeds by failing over to the live key, including the fourth
    # after nine drained-key 402s across the earlier requests: the circuit never
    # opened, and from the second request on the drained keys are not even
    # retried (first request: 4 attempts; the rest: 1 each).
    assert [r.status_code for r in responses] == [200, 200, 200, 200]
    assert calls.count("live") >= 4
    assert calls[:4] == ["k1", "k2", "k3", "live"]


def test_healthy_credentials_serve_while_others_are_drained() -> None:
    """A full route keeps serving while any eligible key remains.

    The mirror of the exclusion test: with three drained and two healthy, the
    healthy keys carry all traffic and requests keep succeeding - the router
    fails over per attempt and never reports the model unavailable.
    """
    drained = {"d1", "d2", "d3"}
    served_by: list[str] = []

    def handler(request: httpx.Request):
        key = _sent_key(request)
        served_by.append(key)
        if key in drained:
            return httpx.Response(402, json=BUDGET_POOL_402)
        return httpx.Response(200, json={"type": "message", "model": "upstream-x"})

    runtime, key = make_pool_runtime(
        handler,
        credential_ids=["d1", "d2", "d3", "h1", "h2"],
        priorities={"d1": 10, "d2": 11, "d3": 12, "h1": 20, "h2": 21},
        max_attempts=5,
    )
    app = create_app(Settings(environment="test", _env_file=None), runtime)

    statuses = []
    with TestClient(app) as client:
        for _ in range(5):
            statuses.append(
                client.post(
                    "/v1/messages",
                    headers={"x-api-key": key},
                    json={"model": "alias-x", "messages": []},
                ).status_code
            )

    assert statuses == [200] * 5
    # First request walked the drained keys before failing over to a healthy one;
    # every later request goes straight to a healthy key (h1 outranks h2, so the
    # deterministic order never needs h2).
    assert served_by[:4] == ["d1", "d2", "d3", "h1"]
    assert set(served_by) == {"d1", "d2", "d3", "h1"}


def test_full_stockout_fails_fast_with_a_truthful_reason() -> None:
    """Every key drained: zero wasted attempts, and a message that says so.

    With all credentials remembered as stock-out, the engine finds no candidate
    and the request refuses immediately (one SQL-free selection, no upstream
    call) with a reason that names the budget pool - not a generic
    no_eligible_route that sends the operator hunting through health states.
    """
    calls = 0

    def handler(request: httpx.Request):
        nonlocal calls
        calls += 1
        return httpx.Response(402, json=BUDGET_POOL_402)

    runtime, key = make_pool_runtime(
        handler,
        credential_ids=["k1", "k2", "k3"],
        priorities={"k1": 10, "k2": 11, "k3": 12},
    )
    app = create_app(Settings(environment="test", _env_file=None), runtime)

    with TestClient(app) as client:
        first = client.post(
            "/v1/messages",
            headers={"x-api-key": key},
            json={"model": "alias-x", "messages": []},
        )
        calls_after_first = calls
        second = client.post(
            "/v1/messages",
            headers={"x-api-key": key},
            json={"model": "alias-x", "messages": []},
        )

    # First request: three attempts, one per credential, all 402.
    assert calls_after_first == 3
    assert first.status_code == 503
    assert first.json()["error"]["type"] == "budget_pool_stockout"
    assert "budget pool" in first.json()["error"]["message"].lower()

    # Second request: every credential is remembered drained for this route, so
    # the request fails with zero upstream calls and says why.
    assert calls == calls_after_first
    assert second.status_code == 503
    assert second.json()["error"]["type"] == "budget_pool_stockout"
    assert "budget-pool" in second.json()["error"]["message"]


def test_stockout_memory_expires_and_one_retest_follows() -> None:
    """A remembered stock-out is not permanent: the TTL earns one re-test.

    The drain clears when the provider releases the next batch (measured
    AgentRouter schedule: Beijing 07:00/19:00), so the memory must expire. After
    the TTL the credential is eligible again for exactly one attempt on the
    route: a success clears the memory for good, a renewed 402 re-arms it.
    """
    from gateway.routing.live_state import _STOCKOUT_TTL_SECONDS

    assert _STOCKOUT_TTL_SECONDS > 0  # a real TTL, not a permanent exile

    drained_now = {"k1"}
    calls: list[str] = []

    def handler(request: httpx.Request):
        key = _sent_key(request)
        calls.append(key)
        if key in drained_now:
            return httpx.Response(402, json=BUDGET_POOL_402)
        return httpx.Response(200, json={"type": "message", "model": "upstream-x"})

    runtime, key = make_pool_runtime(
        handler,
        credential_ids=["k1", "k2"],
        priorities={"k1": 10, "k2": 20},
    )
    app = create_app(Settings(environment="test", _env_file=None), runtime)
    drained_pair = ("k1", "provider-model-x")

    with TestClient(app) as client:
        live = app.state.live_state  # replaced by the lifespan; read inside
        first = client.post(
            "/v1/messages",
            headers={"x-api-key": key},
            json={"model": "alias-x", "messages": []},
        )
        assert first.status_code == 200  # k1's 402 failed over to k2
        assert drained_pair in live.stockout_keys
        # While remembered, k1 is not retried on this route.
        before = len(calls)
        client.post(
            "/v1/messages", headers={"x-api-key": key}, json={"model": "alias-x", "messages": []}
        )
        assert "k1" not in calls[before:]

        # Expire the memory the way time passing would.
        live._stockouts[drained_pair] = (
            datetime.now(UTC) - timedelta(seconds=1)
        )
        # The provider refilled the batch in the meantime.
        drained_now.clear()
        client.post(
            "/v1/messages", headers={"x-api-key": key}, json={"model": "alias-x", "messages": []}
        )
        # k1 was re-tested, succeeded, and the memory is gone.
        assert "k1" in calls[before:]
        assert drained_pair not in live.stockout_keys


def test_failover_can_be_disabled_without_changing_route_configuration() -> None:
    calls = 0

    def handler(request: httpx.Request):
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("unavailable", request=request)

    runtime, key = make_runtime(handler)
    app = create_app(
        Settings(environment="test", failover_enabled=False, _env_file=None), runtime
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            headers={"x-api-key": key},
            json={"model": "alias-x", "messages": []},
        )

    assert response.status_code == 503
    assert calls == 1


def test_upstream_authentication_failure_returns_bad_gateway() -> None:
    runtime, key = make_runtime(
        lambda _: httpx.Response(401, json={"error": {"message": "bad provider key"}})
    )
    app = create_app(Settings(environment="test", _env_file=None), runtime)

    with TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            headers={"x-api-key": key},
            json={"model": "alias-x", "messages": []},
        )

    assert response.status_code == 502
    assert response.json()["error"]["type"] == "upstream_authentication_error"


def make_multi_credential_runtime(handler):
    """Runtime with two credentials on one provider, to exercise credential failover."""
    hasher = GatewayKeyHasher(b"p" * 32)
    issued = hasher.issue(key_id="key-1", client_id="client-1")
    client = GatewayClient(
        id="client-1",
        name="test",
        permissions=ClientPermissions(frozenset({ClientProtocol.ANTHROPIC_MESSAGES})),
    )
    store = InMemoryGatewayKeyStore([issued.record], [client])
    capabilities = frozenset({Capability.STREAMING})
    model = CanonicalModel("model-x", frozenset({"alias-x"}), capabilities)
    provider_model = ProviderModel(
        "provider-model-x",
        "model-x",
        "provider-a",
        "upstream-x",
        ClientProtocol.ANTHROPIC_MESSAGES,
        capabilities,
    )
    registry = ModelRegistry([model], [provider_model])
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
        key_store=store,
        key_hasher=hasher,
        model_registry=registry,
        routing_engine=RoutingEngine(registry),
        provider_states=(ProviderState("provider-a"),),
        credential_states=(
            # Explicit priorities: credential-a is the primary, so the failover
            # order is deterministic and operator intent is preserved.
            CredentialState("credential-a", "provider-a", priority=10),
            CredentialState("credential-b", "provider-a", priority=20),
        ),
        provider_model_adapters={"provider-model-x": adapter},
        credentials={
            "credential-a": Credential(id="credential-a", secret="blocked-key"),
            "credential-b": Credential(id="credential-b", secret="good-key"),
        },
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return runtime, issued.plaintext


def test_upstream_403_fails_over_to_another_credential() -> None:
    """A single upstream-blocked credential must not take the whole provider down.

    Regression test: upstream 401/403 used to be non-retryable, so the executor
    stopped after one attempt and returned 502 Bad Gateway even when other healthy
    credentials for the same provider were available.
    """
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.headers.get("x-api-key") or request.headers.get("authorization", "")
        seen.append(key)
        if "blocked-key" in key:
            return httpx.Response(403, json={"error": {"message": "credential rejected"}})
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={"content": [{"type": "text", "text": "ok"}]},
        )

    runtime, key = make_multi_credential_runtime(handler)
    app = create_app(Settings(environment="test", _env_file=None), runtime)

    with TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            headers={"x-api-key": key},
            json={"model": "alias-x", "messages": []},
        )

    assert response.status_code == 200, response.text
    assert len(seen) == 2, f"expected failover to a second credential, saw {seen}"
    assert any("blocked-key" in k for k in seen)
    assert any("good-key" in k for k in seen)


def test_all_credentials_blocked_still_reports_upstream_auth_error() -> None:
    """When every credential is rejected, the client gets a clear 502 (not a hang)."""

    attempts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request.headers.get("x-api-key", ""))
        return httpx.Response(403, json={"error": {"message": "credential rejected"}})

    runtime, key = make_multi_credential_runtime(handler)
    app = create_app(Settings(environment="test", _env_file=None), runtime)

    with TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            headers={"x-api-key": key},
            json={"model": "alias-x", "messages": []},
        )

    assert response.status_code == 502
    assert response.json()["error"]["type"] == "upstream_authentication_error"
    # Both credentials tried, bounded by max_attempts.
    assert len(attempts) == 2, attempts
