import httpx
import pytest

from gateway.protocols import Capability, ClientProtocol, normalize_request
from gateway.providers.anthropic import AnthropicCompatibleAdapter
from gateway.providers.base import Credential, ErrorCategory, ProviderConfig


def make_adapter(*, capabilities: frozenset[Capability] | None = None):
    config = ProviderConfig(
        id="provider-1",
        name="Example Anthropic Provider",
        base_url="https://provider.example/api",
        protocol=ClientProtocol.ANTHROPIC_MESSAGES,
        capabilities=capabilities
        if capabilities is not None
        else frozenset({Capability.STREAMING, Capability.TOOL_CALLING}),
        timeout_seconds=90,
    )
    return AnthropicCompatibleAdapter(
        config,
        default_headers={"user-agent": "ai-gateway/0.1"},
        required_betas={"required-beta"},
    )


def test_create_request_preserves_payload_and_controls_authentication() -> None:
    adapter = make_adapter()
    payload = {
        "model": "claude-example",
        "stream": True,
        "tools": [{"name": "lookup"}],
        "messages": [{"role": "user", "content": "hello"}],
    }
    normalized = normalize_request(ClientProtocol.ANTHROPIC_MESSAGES, payload)

    request = adapter.create_request(
        normalized,
        Credential(id="credential-1", secret="upstream-secret"),
        {
            "Authorization": "Bearer client-secret",
            "Anthropic-Beta": "client-beta",
            "Anthropic-Version": "2024-01-01",
        },
    )

    assert request.url == "https://provider.example/api/v1/messages"
    assert request.json_body == payload
    assert request.headers["x-api-key"] == "upstream-secret"
    assert request.headers["user-agent"] == "ai-gateway/0.1"
    assert "authorization" not in request.headers
    assert request.headers["anthropic-beta"] == "client-beta,required-beta"
    assert request.headers["anthropic-version"] == "2024-01-01"
    assert request.headers["accept"] == "text/event-stream"


def test_create_request_rejects_missing_capability() -> None:
    adapter = make_adapter(capabilities=frozenset())
    request = normalize_request(
        ClientProtocol.ANTHROPIC_MESSAGES,
        {"model": "claude-example", "stream": True, "messages": []},
    )

    with pytest.raises(ValueError, match="streaming"):
        adapter.create_request(request, Credential(id="credential-1", secret="secret"))


def test_default_headers_cannot_override_runtime_credential() -> None:
    adapter = make_adapter()
    adapter.default_headers["x-api-key"] = "plaintext-setting"
    request = adapter.create_request(
        normalize_request(
            ClientProtocol.ANTHROPIC_MESSAGES,
            {"model": "claude-example", "messages": []},
        ),
        Credential(id="credential-1", secret="upstream-secret"),
    )

    assert request.headers["x-api-key"] == "upstream-secret"


def test_probe_request_uses_a_low_cost_messages_request() -> None:
    request = make_adapter().create_probe_request(
        Credential(id="credential", secret="upstream-secret"),
        model="claude-example",
    )

    assert request.method == "POST"
    assert request.url == "https://provider.example/api/v1/messages"
    assert request.json_body == {
        "model": "claude-example",
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "ping"}],
    }
    assert request.headers["x-api-key"] == "upstream-secret"
    assert request.headers["user-agent"] == "ai-gateway/0.1"
    assert request.headers["anthropic-version"] == "2023-06-01"
    assert request.headers["content-type"] == "application/json"
    assert request.timeout.read == 10


def test_probe_requires_an_upstream_model() -> None:
    with pytest.raises(ValueError, match="upstream model"):
        make_adapter().create_probe_request(
            Credential(id="credential", secret="upstream-secret")
        )


@pytest.mark.parametrize(
    ("status", "payload", "category", "retryable"),
    [
        (
            401,
            {"error": {"message": "bad key"}},
            ErrorCategory.UPSTREAM_AUTHENTICATION_ERROR,
            True,
        ),
        (
            403,
            "<!doctype html><html><title>Cloudflare</title></html>",
            ErrorCategory.UPSTREAM_WAF_REJECTION,
            True,
        ),
        (
            403,
            "",
            ErrorCategory.UPSTREAM_WAF_REJECTION,
            True,
        ),
        (
            403,
            {"error": {"message": "credential rejected"}},
            ErrorCategory.UPSTREAM_AUTHENTICATION_ERROR,
            True,
        ),
        (429, {"error": {"message": "rate limited"}}, ErrorCategory.RATE_LIMIT, True),
        (
            402,
            {"error": {"message": "Budget pool quota has been exhausted."}},
            ErrorCategory.QUOTA_EXHAUSTED,
            True,
        ),
        (429, {"error": {"message": "quota exhausted"}}, ErrorCategory.QUOTA_EXHAUSTED, True),
        (500, {"error": {"message": "unavailable"}}, ErrorCategory.PROVIDER_UNAVAILABLE, True),
        (404, {"error": {"message": "model not found"}}, ErrorCategory.MODEL_UNAVAILABLE, True),
        (400, {"error": {"message": "invalid body"}}, ErrorCategory.INVALID_REQUEST, False),
    ],
)
def test_error_normalization(status, payload, category, retryable) -> None:
    response = (
        httpx.Response(
            status,
            text=payload,
            headers={"content-type": "text/html", "server": "cloudflare"},
        )
        if isinstance(payload, str)
        else httpx.Response(status, json=payload, headers={"retry-after": "2"})
    )

    error = make_adapter().normalize_error(response)

    assert error.category is category
    assert error.retryable is retryable
    assert error.retry_after_seconds == (None if isinstance(payload, str) else 2)


def test_non_json_error_does_not_reflect_upstream_body() -> None:
    response = httpx.Response(502, text="secret internal upstream response")

    error = make_adapter().normalize_error(response)

    assert error.message == "Upstream provider request failed."
    assert "secret" not in error.message
