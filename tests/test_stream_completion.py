import pytest

from gateway.protocols import ClientProtocol
from gateway.protocols.stream_completion import StreamCompletion


@pytest.mark.parametrize("protocol,terminal", [
    (ClientProtocol.ANTHROPIC_MESSAGES, b'data: {"type":"message_stop"}\n\n'),
    (ClientProtocol.OPENAI_RESPONSES, b'data: {"type":"response.completed"}\n\n'),
    (ClientProtocol.OPENAI_CHAT_COMPLETIONS, b'data: [DONE]\n\n'),
])
def test_only_complete_protocol_terminal_is_success_and_error_overrides_it(protocol, terminal):
    completion = StreamCompletion(protocol)
    completion.feed(b'data: {"type":"content_block_delta","delta":{"text":"partial"}}\n\n')
    assert not completion.successful
    completion.feed(terminal[:8])
    assert not completion.successful
    completion.feed(terminal[8:])
    assert completion.successful
    completion.feed(b'data: {"type":"error","error":{"message":"upstream failed"}}\n\n')
    assert not completion.successful
