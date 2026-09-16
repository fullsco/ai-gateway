import pytest

from gateway.protocols import ClientProtocol
from gateway.protocols.output_limits import output_limit, with_output_limit


@pytest.mark.parametrize("protocol,body", [
    (ClientProtocol.OPENAI_RESPONSES, {"max_completion_tokens": 1, "max_output_tokens": 32768}),
    (ClientProtocol.OPENAI_CHAT_COMPLETIONS, {"max_output_tokens": 1}),
    (ClientProtocol.ANTHROPIC_MESSAGES, {"max_completion_tokens": 1}),
    (ClientProtocol.OPENAI_CHAT_COMPLETIONS, {"max_tokens": 1, "max_completion_tokens": 32768}),
])
def test_foreign_or_conflicting_output_fields_are_rejected(protocol, body):
    with pytest.raises(ValueError):
        output_limit(protocol, body, 100)


@pytest.mark.parametrize("protocol,key", [
    (ClientProtocol.OPENAI_RESPONSES, "max_output_tokens"),
    (ClientProtocol.OPENAI_CHAT_COMPLETIONS, "max_tokens"),
    (ClientProtocol.ANTHROPIC_MESSAGES, "max_tokens"),
])
def test_absent_output_limit_is_explicit_and_input_is_not_mutated(protocol, key):
    original = {"model": "synthetic"}
    copied, limit = with_output_limit(protocol, original, 123)
    assert copied[key] == limit == 123
    assert original == {"model": "synthetic"}
