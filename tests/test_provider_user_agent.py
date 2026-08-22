"""Every adapter must identify itself, on every protocol.

Sending no user-agent lets httpx supply "python-httpx/x.y", which Cloudflare
refuses as a generic library. The Anthropic adapter always set one; the OpenAI
adapter did not, so the same provider behaved differently depending only on which
protocol was used: anthropic_messages worked and openai_chat_completions received a
Cloudflare challenge page, which then failed the JSON check and surfaced as a 502.

Measured against gorouter.app and tabitoken.com from the gateway host: the httpx
default, python-requests/2.32.0 and curl/8.5.0 are all refused, while
"ai-gateway/0.1" reaches the backend and returns a JSON 401 for a bad key. The
network path was never the problem, and this was misdiagnosed twice as an IP block.
"""

import pytest

from gateway.protocols import Capability, ClientProtocol, NormalizedRequest
from gateway.providers import Credential, ProviderConfig
from gateway.providers.anthropic import AnthropicCompatibleAdapter
from gateway.providers.openai import OpenAICompatibleAdapter

PROTOCOLS = (
    ClientProtocol.ANTHROPIC_MESSAGES,
    ClientProtocol.OPENAI_CHAT_COMPLETIONS,
    ClientProtocol.OPENAI_RESPONSES,
)


def build(protocol: ClientProtocol, **kwargs):
    config = ProviderConfig(
        id="p",
        name="P",
        base_url="https://upstream.example",
        protocol=protocol,
        capabilities=frozenset({Capability.STREAMING}),
    )
    if protocol is ClientProtocol.ANTHROPIC_MESSAGES:
        return AnthropicCompatibleAdapter(config, **kwargs)
    return OpenAICompatibleAdapter(config, **kwargs)


def prepared(adapter, protocol: ClientProtocol):
    """The upstream request the adapter would actually send."""
    request = NormalizedRequest(
        protocol=protocol,
        requested_model="model-x",
        stream=False,
        required_capabilities=frozenset(),
        payload={"model": "model-x", "max_tokens": 8, "messages": []},
    )
    return adapter.create_request(request, Credential(id="c", secret="secret-value"))


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_every_protocol_sends_an_explicit_user_agent(protocol: ClientProtocol) -> None:
    user_agent = prepared(build(protocol), protocol).headers.get("user-agent", "")
    assert user_agent, f"{protocol.value} sends no user-agent, so httpx supplies its own"
    assert "httpx" not in user_agent.lower()
    assert "python" not in user_agent.lower()


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_a_provider_may_still_override_the_user_agent(protocol: ClientProtocol) -> None:
    """AgentRouter needs a claude-cli user-agent, so the default must not win."""
    adapter = build(protocol, default_headers={"user-agent": "claude-cli/2.1.231"})
    assert prepared(adapter, protocol).headers["user-agent"] == "claude-cli/2.1.231"


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_the_secret_is_never_placed_in_the_user_agent(protocol: ClientProtocol) -> None:
    headers = prepared(build(protocol), protocol).headers
    assert "secret-value" not in headers.get("user-agent", "")
