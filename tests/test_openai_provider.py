import httpx
import pytest

from gateway.protocols import Capability, ClientProtocol, normalize_request
from gateway.providers import Credential, ErrorCategory, ProviderConfig, RetryScope
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

    # GET, not HEAD: relays commonly answer HEAD /v1/models with 404, which made
    # every OpenAI-protocol probe register as a failure.
    assert request.method == "GET"
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
    # Quota is per-credential: a sibling key may still have budget, so the request
    # fails over to another credential rather than failing outright.
    assert error.retryable is True
    assert error.retry_scope is RetryScope.CREDENTIAL


def test_openai_upstream_authentication_error_is_distinct_from_client_auth() -> None:
    response = httpx.Response(401, json={"error": {"message": "bad provider key"}})

    error = make_adapter().normalize_error(response)

    assert error.category is ErrorCategory.UPSTREAM_AUTHENTICATION_ERROR
    assert error.category is not ErrorCategory.AUTHENTICATION_ERROR
    # Retryable so a single upstream-blocked credential fails over to another
    # credential instead of failing the whole request with 502.
    assert error.retryable is True


def test_streamed_chat_completions_requests_usage() -> None:
    """Chat Completions omits usage over SSE unless the caller opts in.

    Without it every streamed request produced no usage record and no cost.
    """
    request = normalize_request(
        ClientProtocol.OPENAI_CHAT_COMPLETIONS,
        {"model": "model-x", "stream": True, "messages": []},
    )

    adapter = make_adapter(ClientProtocol.OPENAI_CHAT_COMPLETIONS)
    upstream = adapter.create_request(request, Credential(id="c", secret="s"))

    assert upstream.json_body["stream_options"] == {"include_usage": True}


def test_non_streamed_request_is_forwarded_unchanged() -> None:
    request = normalize_request(
        ClientProtocol.OPENAI_CHAT_COMPLETIONS,
        {"model": "model-x", "messages": []},
    )

    adapter = make_adapter(ClientProtocol.OPENAI_CHAT_COMPLETIONS)
    upstream = adapter.create_request(request, Credential(id="c", secret="s"))

    assert "stream_options" not in upstream.json_body


def test_caller_supplied_stream_options_are_respected() -> None:
    request = normalize_request(
        ClientProtocol.OPENAI_CHAT_COMPLETIONS,
        {
            "model": "model-x",
            "stream": True,
            "messages": [],
            "stream_options": {"include_usage": False},
        },
    )

    adapter = make_adapter(ClientProtocol.OPENAI_CHAT_COMPLETIONS)
    upstream = adapter.create_request(request, Credential(id="c", secret="s"))

    assert upstream.json_body["stream_options"] == {"include_usage": False}


def test_edge_challenge_403_is_not_blamed_on_the_credential() -> None:
    """A Cloudflare 403 challenge page is not a credential rejection.

    Without this branch every working key on the provider was parked in turn,
    because each 403 marked the credential auth_failed.
    """
    error = make_adapter().normalize_error(
        httpx.Response(
            403,
            headers={"content-type": "text/html"},
            text="<html><title>Attention Required! | Cloudflare</title></html>",
        )
    )

    assert error.category is ErrorCategory.UPSTREAM_WAF_REJECTION
    assert error.credential_at_fault is False


def test_genuine_403_error_envelope_is_still_a_credential_rejection() -> None:
    error = make_adapter().normalize_error(
        httpx.Response(403, json={"error": {"message": "invalid api key"}})
    )

    assert error.category is ErrorCategory.UPSTREAM_AUTHENTICATION_ERROR
    assert error.credential_at_fault is True


def test_payment_required_without_a_marker_is_a_billing_condition() -> None:
    error = make_adapter().normalize_error(
        httpx.Response(402, json={"error": {"message": "top up"}})
    )

    assert error.category is ErrorCategory.QUOTA_EXHAUSTED
    assert error.retryable is True
