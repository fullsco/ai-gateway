import base64
import json
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.config import Settings
from gateway.configuration import RuntimeBuilder
from gateway.health.probes import run_health_probes
from gateway.routing.engine import HealthState
from gateway.security import CredentialCipher, GatewayKeyHasher


def build_runtime(handler, *, restrict_first_credential: bool = False):
    encryption_key = base64.b64encode(b"e" * 32).decode()
    pepper = base64.b64encode(b"p" * 32).decode()
    cipher = CredentialCipher.from_base64(encryption_key)
    hasher = GatewayKeyHasher.from_base64(pepper)
    issued = hasher.issue(key_id="key", client_id="client")
    credentials = []
    for index in (1, 2):
        credential_id = f"credential-{index}"
        envelope = cipher.encrypt(
            f"provider-key-{index}",
            context=f"provider-credential:{credential_id}",
        )
        credentials.append(
            {
                "id": credential_id,
                "provider_id": "provider",
                "secret_nonce": envelope.nonce,
                "secret_ciphertext": envelope.ciphertext,
                "priority": index * 10,
                "supported_provider_model_ids": (
                    ["anthropic-mapping"] if restrict_first_credential and index == 1 else []
                ),
            }
        )
    payload = {
        "clients": [
            {
                "id": "client",
                "name": "client",
                "allowed_protocols": [
                    "anthropic_messages",
                    "openai_chat_completions",
                ],
            }
        ],
        "gateway_keys": [
            {
                "id": "key",
                "client_id": "client",
                "key_prefix": issued.record.key_prefix,
                "key_digest": issued.record.digest,
            }
        ],
        "providers": [
            {
                "id": "provider",
                "name": "Shared provider",
                "base_url": "https://provider.example",
                "capabilities": ["streaming"],
            }
        ],
        "credentials": credentials,
        "models": [
            {"id": "claude-model", "capabilities": ["streaming"]},
            {"id": "openai-model", "capabilities": ["streaming"]},
        ],
        "provider_models": [
            {
                "id": "anthropic-mapping",
                "canonical_model_id": "claude-model",
                "provider_id": "provider",
                "upstream_model_id": "claude-upstream",
                "protocol": "anthropic_messages",
                "capabilities": ["streaming"],
            },
            {
                "id": "openai-mapping",
                "canonical_model_id": "openai-model",
                "provider_id": "provider",
                "upstream_model_id": "openai-upstream",
                "protocol": "openai_chat_completions",
                "capabilities": ["streaming"],
            },
        ],
    }
    runtime = RuntimeBuilder(encryption_key=encryption_key, key_pepper=pepper).build(payload)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    runtime = runtime.__class__(**{**runtime.__dict__, "http_client": client})
    return runtime, issued.plaintext


def build_two_provider_runtime(handler):
    """One model served by two providers, the first holding several credentials.

    The credential count on the first provider deliberately exceeds the attempt
    budget, which is the situation that hid the missing provider-scope handling.
    """
    encryption_key = base64.b64encode(b"e" * 32).decode()
    pepper = base64.b64encode(b"p" * 32).decode()
    cipher = CredentialCipher.from_base64(encryption_key)
    hasher = GatewayKeyHasher.from_base64(pepper)
    issued = hasher.issue(key_id="key", client_id="client")

    credentials = []
    for index in range(1, 6):
        credential_id = f"first-credential-{index}"
        envelope = cipher.encrypt(
            f"first-key-{index}", context=f"provider-credential:{credential_id}"
        )
        credentials.append({
            "id": credential_id, "provider_id": "first",
            "secret_nonce": envelope.nonce, "secret_ciphertext": envelope.ciphertext,
            "priority": index,
        })
    envelope = cipher.encrypt("second-key", context="provider-credential:second-credential")
    credentials.append({
        "id": "second-credential", "provider_id": "second",
        "secret_nonce": envelope.nonce, "secret_ciphertext": envelope.ciphertext,
        "priority": 1,
    })

    def provider(identifier: str, host: str) -> dict:
        return {
            "id": identifier, "name": identifier, "provider_type": "anthropic_compatible",
            "protocol": "anthropic_messages", "base_url": f"https://{host}",
            "enabled": True, "priority": 100, "capabilities": ["streaming"],
            "timeout_seconds": 30, "health": "healthy",
        }

    def mapping(identifier: str, provider_id: str, priority: int) -> dict:
        return {
            "id": identifier, "canonical_model_id": "shared-model",
            "provider_id": provider_id, "upstream_model_id": "shared-model",
            "protocol": "anthropic_messages", "capabilities": ["streaming"],
            "priority": priority, "weight": 1, "enabled": True, "max_concurrency": 8,
        }

    payload = {
        "clients": [{
            "id": "client", "name": "client",
            "allowed_protocols": ["anthropic_messages"],
        }],
        "gateway_keys": [{
            "id": issued.record.id, "client_id": issued.record.client_id,
            "key_prefix": issued.record.key_prefix, "key_digest": issued.record.digest,
            "enabled": True,
        }],
        "providers": [provider("first", "first.example"), provider("second", "second.example")],
        "credentials": credentials,
        "models": [{"id": "shared-model", "enabled": True, "capabilities": ["streaming"]}],
        "provider_models": [
            mapping("first-mapping", "first", 100),
            mapping("second-mapping", "second", 200),
        ],
    }
    runtime = RuntimeBuilder(encryption_key=encryption_key, key_pepper=pepper).build(payload)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    runtime = runtime.__class__(**{**runtime.__dict__, "http_client": client})
    return runtime, issued.plaintext


def test_shared_credentials_route_through_protocol_specific_adapters() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(
            (
                request.url.path,
                body["model"],
                request.headers.get("x-api-key"),
                request.headers.get("authorization"),
            )
        )
        return httpx.Response(200, json={"id": "ok"})

    runtime, key = build_runtime(handler)
    app = create_app(Settings(environment="test", _env_file=None), runtime)

    with TestClient(app) as client:
        anthropic = client.post(
            "/v1/messages",
            headers={"x-api-key": key},
            json={"model": "claude-model", "messages": []},
        )
        openai = client.post(
            "/v1/chat/completions",
            headers={"authorization": f"Bearer {key}"},
            json={"model": "openai-model", "messages": []},
        )

    assert anthropic.status_code == 200
    assert openai.status_code == 200
    assert seen == [
        ("/v1/messages", "claude-upstream", "provider-key-1", None),
        ("/v1/chat/completions", "openai-upstream", None, "Bearer provider-key-1"),
    ]


def test_mapping_protocol_is_exact_and_credential_restrictions_still_apply() -> None:
    seen_keys = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_keys.append(request.headers["authorization"])
        return httpx.Response(200, json={"id": "ok"})

    runtime, key = build_runtime(handler, restrict_first_credential=True)
    app = create_app(Settings(environment="test", _env_file=None), runtime)

    with TestClient(app) as client:
        wrong_protocol = client.post(
            "/v1/messages",
            headers={"x-api-key": key},
            json={"model": "openai-model", "messages": []},
        )
        allowed = client.post(
            "/v1/chat/completions",
            headers={"authorization": f"Bearer {key}"},
            json={"model": "openai-model", "messages": []},
        )

    # No mapping exposes this model over the Anthropic protocol, so no candidate is
    # even considered. That is a configuration condition and is reported as 404
    # model_unavailable, distinct from 503 no_eligible_route, which means the model
    # is servable but every candidate was temporarily ineligible.
    assert wrong_protocol.status_code == 404
    assert wrong_protocol.json()["error"]["type"] == "model_unavailable"
    assert allowed.status_code == 200
    assert seen_keys == ["Bearer provider-key-2"]


def test_credential_failover_rotates_within_selected_mapping() -> None:
    seen_keys = []

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.headers["x-api-key"]
        seen_keys.append(key)
        if key == "provider-key-1":
            return httpx.Response(429, json={"error": {"message": "rate limited"}})
        return httpx.Response(200, json={"id": "ok"})

    runtime, key = build_runtime(handler)
    app = create_app(Settings(environment="test", _env_file=None), runtime)

    with TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            headers={"x-api-key": key},
            json={"model": "claude-model", "messages": []},
        )

    assert response.status_code == 200
    assert seen_keys == ["provider-key-1", "provider-key-2"]


@pytest.mark.asyncio
async def test_health_probes_are_bounded_to_one_route_per_credential() -> None:
    seen_paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        return httpx.Response(200)

    runtime, _ = build_runtime(handler)
    from tests.test_probes import Limiter

    await run_health_probes(runtime, None, Limiter(["reserved:a", "reserved:b"]))
    await runtime.http_client.aclose()

    assert len(seen_paths) == 2
    assert set(seen_paths) <= {"/v1/messages", "/v1/models"}


def test_exhausted_capacity_is_reported_as_no_eligible_route_not_a_missing_model() -> None:
    """A model whose credentials are all unusable is servable, just not right now.

    Both conditions used to return model_unavailable, which sent the operator to
    check mappings when the real cause was credential health.
    """
    runtime, key = build_runtime(lambda _: httpx.Response(200, json={"id": "x"}))
    # Park every credential, exactly as a live failure would.
    runtime = runtime.__class__(
        **{
            **runtime.__dict__,
            "credential_states": tuple(
                replace(state, health=HealthState.UNAVAILABLE)
                for state in runtime.credential_states
            ),
        }
    )
    app = create_app(Settings(environment="test", _env_file=None), runtime)

    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"authorization": f"Bearer {key}"},
            json={"model": "openai-model", "messages": []},
        )

    assert response.status_code == 503
    assert response.json()["error"]["type"] == "no_eligible_route"
    assert "currently eligible" in response.json()["error"]["message"]


def test_a_provider_scoped_failure_moves_to_the_next_provider() -> None:
    """retry_scope was computed and never acted on.

    A provider-level failure only retired the failing credential, so a provider with
    more credentials than the attempt budget consumed every attempt before any
    configured fallback provider was reached. GoRouter has five credentials against
    three attempts, which made its fallback unreachable in practice.
    """
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        # The first provider is unreachable at its edge, which is not the fault of
        # any credential it holds.
        if "first.example" in str(request.url):
            return httpx.Response(503, json={"error": {"message": "unavailable"}})
        return httpx.Response(200, json={"id": "ok", "type": "message"})

    runtime, key = build_two_provider_runtime(handler)
    app = create_app(Settings(environment="test", _env_file=None), runtime)

    with TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            headers={"x-api-key": key},
            json={"model": "shared-model", "messages": []},
        )

    assert response.status_code == 200, response.text
    first = [url for url in seen if "first.example" in url]
    second = [url for url in seen if "second.example" in url]
    assert len(first) == 1, (
        f"the unreachable provider must be retired after one attempt, got {len(first)}"
    )
    assert second, "the second provider must be reached"


def test_a_route_may_tighten_the_provider_timeout_but_not_loosen_it() -> None:
    """One slow model must be boundable without slowing everything else.

    A provider's timeout is shared by every model it serves. hcnsec answers in
    anywhere from 6 to 600 seconds, and at 600 a stalled attempt holds a concurrency
    slot and the caller for ten minutes. Lowering hcnsec's own timeout would also
    bound its other routes, so the ceiling belongs on the mapping. Raising it must
    not be possible, or a single mapping could quietly relax the provider's limit.
    """
    from gateway.configuration.runtime_builder import (
        RuntimeBuilder,
        SnapshotProvider,
        SnapshotProviderModel,
    )
    from gateway.protocols import Capability, ClientProtocol

    provider = SnapshotProvider(
        id="p",
        name="P",
        base_url="https://upstream.example",
        capabilities=frozenset({Capability.STREAMING}),
        timeout_seconds=600,
    )

    def adapter_for(route_timeout: float | None):
        model = SnapshotProviderModel(
            id="pm",
            canonical_model_id="model-x",
            provider_id="p",
            upstream_model_id="upstream-x",
            protocol=ClientProtocol.ANTHROPIC_MESSAGES,
            capabilities=frozenset({Capability.STREAMING}),
            timeout_seconds=route_timeout,
        )
        import base64

        key = base64.b64encode(b"k" * 32).decode()
        builder = RuntimeBuilder(encryption_key=key, key_pepper=key)
        return builder._build_adapter(provider, model)

    assert adapter_for(None).config.timeout_seconds == 600
    assert adapter_for(120).config.timeout_seconds == 120
    # Above the provider's ceiling the provider still wins.
    assert adapter_for(9000).config.timeout_seconds == 600
