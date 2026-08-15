from gateway.protocols import Capability, ClientProtocol, normalize_request


def test_normalization_is_lossless_and_extracts_routing_metadata() -> None:
    payload = {
        "model": "claude-example",
        "stream": True,
        "tools": [{"name": "lookup"}],
        "thinking": {"type": "enabled", "budget_tokens": 1024},
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "thinking",
                        "thinking": "private",
                        "signature": "high-entropy-signature-is-preserved",
                    }
                ],
            }
        ],
    }

    request = normalize_request(ClientProtocol.ANTHROPIC_MESSAGES, payload)

    assert request.payload == payload
    assert request.payload is not payload
    assert request.required_capabilities == {
        Capability.STREAMING,
        Capability.TOOL_CALLING,
        Capability.REASONING,
    }


def test_normalization_requires_model() -> None:
    try:
        normalize_request(ClientProtocol.OPENAI_RESPONSES, {"input": "hello"})
    except ValueError as exc:
        assert str(exc) == "Request model must be a non-empty string"
    else:
        raise AssertionError("Expected request without a model to fail")
