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
