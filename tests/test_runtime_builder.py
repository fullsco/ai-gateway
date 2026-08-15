import base64

import pytest

from gateway.configuration import RuntimeBuilder
from gateway.protocols import ClientProtocol, NormalizedRequest
from gateway.providers import Credential
from gateway.security import CredentialCipher


def encoded(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def make_payload() -> tuple[RuntimeBuilder, dict]:
    encryption_key = encoded(b"e" * 32)
    pepper = encoded(b"p" * 32)
    cipher = CredentialCipher.from_base64(encryption_key)
    envelope = cipher.encrypt("provider-secret", context="provider-credential:credential-1")
    builder = RuntimeBuilder(encryption_key=encryption_key, key_pepper=pepper)
    payload = {
        "clients": [
            {
                "id": "client-1",
                "name": "Claude Code",
                "allowed_protocols": ["anthropic_messages"],
                "requests_per_minute": 10,
                "tokens_per_minute": 1000,
                "spending_limit": 5,
            }
        ],
        "gateway_keys": [
            {
                "id": "key-1",
                "client_id": "client-1",
                "key_prefix": "gw_live_examplex",
                "key_digest": "0" * 64,
            }
        ],
        "providers": [
            {
                "id": "provider-1",
                "name": "Provider",
                "provider_type": "anthropic_compatible",
                "protocol": "anthropic_messages",
                "base_url": "https://provider.example",
                "capabilities": ["streaming"],
                "default_headers": {"user-agent": "claude-cli/test"},
                "auth_scheme": "bearer",
                "endpoint_query": {"beta": "true"},
            }
        ],
        "credentials": [
            {
                "id": "credential-1",
                "provider_id": "provider-1",
                "secret_nonce": envelope.nonce,
                "secret_ciphertext": envelope.ciphertext,
                "quota_limit": 10,
                "quota_used": 2,
            }
        ],
        "models": [{"id": "model-1", "aliases": ["latest"], "capabilities": ["streaming"]}],
        "provider_models": [
            {
                "id": "provider-model-1",
                "canonical_model_id": "model-1",
                "provider_id": "provider-1",
                "upstream_model_id": "upstream-1",
                "protocol": "anthropic_messages",
                "capabilities": ["streaming"],
            }
        ],
    }
    return builder, payload


def test_runtime_builder_decrypts_credentials_and_builds_routes() -> None:
    builder, payload = make_payload()

    runtime = builder.build(payload)

    assert runtime.credentials["credential-1"].secret == "provider-secret"
    client = runtime.key_store.get_client("client-1")
    assert client is not None
    assert client.requests_per_minute == 10
    assert client.tokens_per_minute == 1000
    assert client.spending_limit == 5
    assert runtime.model_registry.resolve("latest").id == "model-1"
    assert runtime.credential_states[0].quota_headroom == 0.8
    provider = runtime.provider_model_adapters["provider-model-1"]
    assert provider.config.protocol is ClientProtocol.ANTHROPIC_MESSAGES
    request = provider.create_request(
        NormalizedRequest(
            protocol=ClientProtocol.ANTHROPIC_MESSAGES,
            requested_model="model-1",
            payload={"model": "upstream-1"},
        ),
        Credential(id="credential-1", secret="provider-secret"),
    )
    assert request.url == "https://provider.example/v1/messages?beta=true"
    assert request.headers["authorization"] == "Bearer provider-secret"
    assert "x-api-key" not in request.headers
    assert request.headers["user-agent"] == "claude-cli/test"


def test_runtime_builder_rejects_unknown_snapshot_fields() -> None:
    builder, payload = make_payload()
    payload["unknown"] = True

    with pytest.raises(ValueError, match="unknown"):
        builder.build(payload)


def test_runtime_builder_rejects_envelope_with_wrong_context() -> None:
    builder, payload = make_payload()
    payload["credentials"][0]["id"] = "different-credential"

    with pytest.raises(ValueError, match="Credential decryption failed"):
        builder.build(payload)


def test_runtime_builder_rejects_credential_bearing_default_headers() -> None:
    builder, payload = make_payload()
    payload["providers"][0]["default_headers"] = {"authorization": "plaintext-secret"}

    with pytest.raises(ValueError, match="credential headers"):
        builder.build(payload)


def test_runtime_builder_rejects_capabilities_missing_from_canonical_model() -> None:
    builder, payload = make_payload()
    payload["provider_models"][0]["capabilities"] = ["streaming", "reasoning"]

    with pytest.raises(ValueError, match="canonical model"):
        builder.build(payload)


def test_mapping_capabilities_are_not_limited_by_legacy_provider_metadata() -> None:
    builder, payload = make_payload()
    payload["providers"][0]["capabilities"] = []

    runtime = builder.build(payload)

    assert runtime.model_registry.list_provider_models()[0].capabilities == {"streaming"}


def test_runtime_builder_rejects_non_http_provider_url() -> None:
    builder, payload = make_payload()
    payload["providers"][0]["base_url"] = "file:///tmp/provider"

    with pytest.raises(ValueError, match="URL scheme"):
        builder.build(payload)


def test_runtime_builder_enforces_credential_model_access() -> None:
    builder, payload = make_payload()
    payload["credentials"][0]["supported_provider_model_ids"] = ["other-route"]
    runtime = builder.build(payload)

    assert runtime.credential_states[0].supported_provider_model_ids == {"other-route"}


def test_runtime_builder_rejects_non_positive_route_concurrency() -> None:
    builder, payload = make_payload()
    payload["provider_models"][0]["max_concurrency"] = 0

    with pytest.raises(ValueError, match="greater than 0"):
        builder.build(payload)


def test_runtime_builder_allows_multiple_protocol_mappings_on_one_provider() -> None:
    builder, payload = make_payload()
    payload["providers"][0]["protocol"] = "anthropic_messages"
    payload["provider_models"].append(
        {
            "id": "provider-model-openai",
            "canonical_model_id": "model-1",
            "provider_id": "provider-1",
            "upstream_model_id": "upstream-openai",
            "protocol": "openai_chat_completions",
            "capabilities": ["streaming"],
        }
    )
    payload["models"][0]["capabilities"] = ["streaming"]

    runtime = builder.build(payload)

    assert set(runtime.provider_model_adapters) == {
        "provider-model-1",
        "provider-model-openai",
    }
    anthropic = runtime.provider_model_adapters["provider-model-1"]
    openai = runtime.provider_model_adapters["provider-model-openai"]
    assert anthropic.config.protocol is ClientProtocol.ANTHROPIC_MESSAGES
    assert openai.config.protocol is ClientProtocol.OPENAI_CHAT_COMPLETIONS
