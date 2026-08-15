import base64
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.config import Settings
from gateway.configuration import RuntimeBuilder
from gateway.health.probes import run_health_probes
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

    assert wrong_protocol.status_code == 503
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
async def test_health_probes_use_each_provider_model_adapter() -> None:
    seen_paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        return httpx.Response(200)

    runtime, _ = build_runtime(handler)
    await run_health_probes(runtime, None)
    await runtime.http_client.aclose()

    assert seen_paths.count("/v1/messages") == 2
    assert seen_paths.count("/v1/models") == 2
