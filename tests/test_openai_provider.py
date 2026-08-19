import httpx
import pytest

from gateway.protocols import Capability, ClientProtocol, normalize_request
from gateway.providers import Credential, ErrorCategory, ProviderConfig
from gateway.providers.openai import OpenAICompatibleAdapter


def make_adapter(protocol=ClientProtocol.OPENAI_RESPONSES):
    return OpenAICompatibleAdapter(
        ProviderConfig(
            id="openai-provider",
            name="OpenAI-compatible",
            base_url="https://provider.example/api",
            protocol=protocol,
            capabilities=frozenset({Capability.STREAMING, Capability.TOOL_CALLING}),
        )
    )


@pytest.mark.parametrize(
    ("protocol", "endpoint"),
    [
        (ClientProtocol.OPENAI_CHAT_COMPLETIONS, "/v1/chat/completions"),
        (ClientProtocol.OPENAI_RESPONSES, "/v1/responses"),
    ],
)
def test_create_request_uses_protocol_endpoint_and_provider_key(protocol, endpoint) -> None:
    normalized = normalize_request(protocol, {"model": "model-x", "stream": True})

    request = make_adapter(protocol).create_request(
        normalized,
        Credential(id="credential", secret="provider-secret"),
        {"authorization": "Bearer client-secret"},
    )

    assert request.url == f"https://provider.example/api{endpoint}"
    assert request.headers["authorization"] == "Bearer provider-secret"
    assert request.headers["accept"] == "text/event-stream"


def test_default_headers_cannot_override_runtime_credential() -> None:
    adapter = make_adapter()
    adapter.default_headers["authorization"] = "Bearer plaintext-setting"
    request = adapter.create_request(
        normalize_request(ClientProtocol.OPENAI_RESPONSES, {"model": "model-x"}),
        Credential(id="credential", secret="provider-secret"),
    )

    assert request.headers["authorization"] == "Bearer provider-secret"


def test_probe_request_is_authenticated_and_payload_free() -> None:
    request = make_adapter().create_probe_request(
        Credential(id="credential", secret="provider-secret")
    )

    assert request.method == "HEAD"
    assert request.url == "https://provider.example/api/v1/models"
    assert request.json_body is None
    assert request.headers["authorization"] == "Bearer provider-secret"
    assert request.timeout.read == 10


def test_adapter_rejects_other_openai_protocol() -> None:
    request = normalize_request(
        ClientProtocol.OPENAI_CHAT_COMPLETIONS,
        {"model": "model-x"},
    )

    with pytest.raises(ValueError, match="requested OpenAI protocol"):
        make_adapter(ClientProtocol.OPENAI_RESPONSES).create_request(
            request,
            Credential(id="credential", secret="secret"),
        )


def test_openai_quota_error_is_normalized() -> None:
    response = httpx.Response(
        402,
        json={
            "error": {
                "message": "Budget pool quota has been exhausted.",
                "type": "insufficient_quota",
                "code": "insufficient_quota",
            }
        },
    )

    error = make_adapter().normalize_error(response)

    assert error.category is ErrorCategory.QUOTA_EXHAUSTED
    assert error.retryable is False


def test_openai_upstream_authentication_error_is_distinct_from_client_auth() -> None:
    response = httpx.Response(401, json={"error": {"message": "bad provider key"}})

    error = make_adapter().normalize_error(response)

    assert error.category is ErrorCategory.UPSTREAM_AUTHENTICATION_ERROR
    assert error.category is not ErrorCategory.AUTHENTICATION_ERROR
    # Retryable so a single upstream-blocked credential fails over to another
    # credential instead of failing the whole request with 502.
    assert error.retryable is True
