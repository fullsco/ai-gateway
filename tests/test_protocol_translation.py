"""Unit tests for the protocol translation matrix.

Everything here is pure: a translator is fed a payload and its output is compared field
by field. The streaming half lives in `test_protocol_translation_streams.py` and the
HTTP path in `test_translated_routes.py`.
"""

import json

from gateway.protocols.translate import can_translate, get_translator, translatable_pairs
from tests.translation_support import (
    ANTHROPIC,
    CHAT,
    CONTEXT,
    RESPONSES,
    THINKING_CONTEXT,
    translator,
)

# --------------------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------------------


def test_every_ordered_pair_of_protocols_is_translatable() -> None:
    """A route must be reachable from any client protocol, or the 404 wall remains."""
    protocols = (ANTHROPIC, CHAT, RESPONSES)
    for client in protocols:
        for upstream in protocols:
            assert can_translate(client, upstream), f"{client.value} <- {upstream.value}"

    # Six ordered pairs, and an identity pair is not one of them: it means "relay the
    # bytes", which is how the executor keeps today's verbatim path.
    assert len(translatable_pairs()) == 6
    for protocol in protocols:
        assert get_translator(protocol, protocol) is None


def test_a_chained_pair_reports_the_endpoints_not_the_intermediate() -> None:
    chained = translator(ANTHROPIC, RESPONSES)
    assert chained.client_protocol is ANTHROPIC
    assert chained.upstream_protocol is RESPONSES


# --------------------------------------------------------------------------------------
# Anthropic Messages <- Chat Completions: request
# --------------------------------------------------------------------------------------


def test_anthropic_request_becomes_chat_completions() -> None:
    upstream = translator(ANTHROPIC, CHAT).translate_request(
        {
            "model": "glm-5.3",
            "max_tokens": 512,
            "temperature": 0.2,
            "stream": True,
            "system": [
                {"type": "text", "text": "Be brief."},
                {"type": "text", "text": "Cite files."},
            ],
            "stop_sequences": ["</done>"],
            "thinking": {"type": "enabled", "budget_tokens": 4096},
            "metadata": {"user_id": "operator-1"},
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "What is this?"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": "aGk=",
                            },
                        },
                    ],
                }
            ],
            "tools": [
                {
                    "name": "read_file",
                    "description": "Read a file",
                    "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
                },
                # A server tool has no Chat Completions equivalent and must not be sent
                # as a function the upstream would reject.
                {"type": "web_search_20250305", "name": "web_search"},
            ],
            "tool_choice": {"type": "tool", "name": "read_file"},
        }
    )

    assert upstream["model"] == "glm-5.3"
    assert upstream["max_tokens"] == 512
    assert upstream["temperature"] == 0.2
    # `stream` must survive translation or a streamed request is silently answered whole.
    assert upstream["stream"] is True
    assert upstream["stop"] == ["</done>"]
    assert upstream["reasoning_effort"] == "medium"
    assert upstream["user"] == "operator-1"
    assert upstream["messages"] == [
        {"role": "system", "content": "Be brief.\n\nCite files."},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "What is this?"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGk="}},
            ],
        },
    ]
    assert upstream["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            },
        }
    ]
    assert upstream["tool_choice"] == {"type": "function", "function": {"name": "read_file"}}


def test_a_tool_result_precedes_the_rest_of_its_anthropic_turn() -> None:
    """OpenAI requires every tool message to follow the assistant turn that asked.

    Anthropic carries the result as a content block inside the *next user* turn, so
    leaving it inline would place it after whatever else that turn said and break the
    agent loop on a strict upstream.
    """
    upstream = translator(ANTHROPIC, CHAT).translate_request(
        {
            "model": "glm-5.3",
            "messages": [
                {"role": "user", "content": "weather in Paris?"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "private", "signature": "sig"},
                        {"type": "text", "text": "Checking."},
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "get_weather",
                            "input": {"city": "Paris"},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "18C"},
                        {"type": "text", "text": "and tomorrow?"},
                    ],
                },
            ],
        }
    )

    assert upstream["messages"] == [
        {"role": "user", "content": "weather in Paris?"},
        {
            "role": "assistant",
            "content": "Checking.",
            "tool_calls": [
                {
                    "id": "toolu_1",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city":"Paris"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "toolu_1", "content": "18C"},
        {"role": "user", "content": "and tomorrow?"},
    ]
    # The thinking block is dropped rather than replayed as assistant speech.
    assert "private" not in json.dumps(upstream)


# --------------------------------------------------------------------------------------
# Anthropic Messages <- Chat Completions: response
# --------------------------------------------------------------------------------------


def test_chat_completion_becomes_an_anthropic_message() -> None:
    translated = translator(ANTHROPIC, CHAT).translate_response(
        {
            "id": "chatcmpl-77",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": "Reading it now.",
                        "reasoning_content": "the user wants the file",
                        "tool_calls": [
                            {
                                "id": "call_a",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": '{"path":"a.py"}',
                                },
                            }
                        ],
                    },
                }
            ],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "prompt_tokens_details": {"cached_tokens": 30},
            },
        },
        THINKING_CONTEXT,
    )

    assert translated["type"] == "message"
    assert translated["role"] == "assistant"
    # The canonical name the client asked for, not the upstream model id: echoing the
    # latter leaks the routing decision into the client's transcript.
    assert translated["model"] == "glm-5.3"
    assert translated["id"] == "msg_chatcmpl-77"
    assert translated["content"] == [
        {"type": "thinking", "thinking": "the user wants the file", "signature": ""},
        {"type": "text", "text": "Reading it now."},
        {"type": "tool_use", "id": "call_a", "name": "read_file", "input": {"path": "a.py"}},
    ]
    # The upstream said `stop` alongside a tool call. An Anthropic agent loop keys on
    # stop_reason, so end_turn here would silently drop the call it just received.
    assert translated["stop_reason"] == "tool_use"
    assert translated["stop_sequence"] is None
    # OpenAI's prompt total includes cached tokens; Anthropic's input_tokens excludes
    # them, so the client's own `input + cache_read` arithmetic still lands on 100.
    assert translated["usage"] == {
        "input_tokens": 70,
        "output_tokens": 20,
        "cache_read_input_tokens": 30,
    }


def test_upstream_reasoning_is_withheld_unless_thinking_was_requested() -> None:
    payload = {
        "choices": [
            {
                "finish_reason": "length",
                "message": {"content": "partial", "reasoning_content": "private"},
            }
        ]
    }
    without = translator(ANTHROPIC, CHAT).translate_response(payload, CONTEXT)

    assert without["content"] == [{"type": "text", "text": "partial"}]
    assert without["stop_reason"] == "max_tokens"
    assert without["usage"] == {"input_tokens": 0, "output_tokens": 0}


def test_unparseable_tool_arguments_are_surfaced_not_dropped() -> None:
    """A malformed call must be visible, not arrive as an empty one."""
    translated = translator(ANTHROPIC, CHAT).translate_response(
        {
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "tool_calls": [
                            {"id": "c1", "function": {"name": "f", "arguments": "{not json"}}
                        ]
                    },
                }
            ]
        },
        CONTEXT,
    )

    assert translated["content"] == [
        {
            "type": "tool_use",
            "id": "c1",
            "name": "f",
            "input": {"__unparsed_arguments": "{not json"},
        }
    ]


# --------------------------------------------------------------------------------------
# Chat Completions <- Anthropic Messages
# --------------------------------------------------------------------------------------


def test_chat_request_becomes_anthropic_messages() -> None:
    upstream = translator(CHAT, ANTHROPIC).translate_request(
        {
            "model": "claude-opus-5",
            "messages": [
                {"role": "system", "content": "Be brief."},
                {"role": "user", "content": "call both tools"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": "call_a", "function": {"name": "f", "arguments": '{"x":1}'}},
                        {"id": "call_b", "function": {"name": "g", "arguments": "{}"}},
                    ],
                },
                {"role": "tool", "tool_call_id": "call_a", "content": "one"},
                {"role": "tool", "tool_call_id": "call_b", "content": "two"},
            ],
            "stop": "END",
            "reasoning_effort": "high",
            "user": "operator-1",
        }
    )

    assert upstream["system"] == "Be brief."
    # Anthropic rejects a request without max_tokens, and Chat Completions clients
    # routinely omit it, so the substitute has to be generous enough not to truncate.
    assert upstream["max_tokens"] == 8192
    assert upstream["stop_sequences"] == ["END"]
    assert upstream["thinking"] == {"type": "enabled", "budget_tokens": 16384}
    assert upstream["metadata"] == {"user_id": "operator-1"}
    # Two tool results collapse into a single user turn, which is what Anthropic
    # requires when a model called several tools at once.
    assert upstream["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "call both tools"}]},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "call_a", "name": "f", "input": {"x": 1}},
                {"type": "tool_use", "id": "call_b", "name": "g", "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "call_a", "content": "one"},
                {"type": "tool_result", "tool_use_id": "call_b", "content": "two"},
            ],
        },
    ]


def test_anthropic_message_becomes_a_chat_completion() -> None:
    translated = translator(CHAT, ANTHROPIC).translate_response(
        {
            "id": "msg_9",
            "stop_reason": "tool_use",
            "content": [
                {"type": "thinking", "thinking": "deliberating", "signature": "sig"},
                {"type": "text", "text": "Calling."},
                {"type": "tool_use", "id": "toolu_1", "name": "f", "input": {"x": 1}},
            ],
            "usage": {
                "input_tokens": 70,
                "output_tokens": 20,
                "cache_read_input_tokens": 30,
            },
        },
        CONTEXT,
    )

    assert translated["id"] == "chatcmpl-msg_9"
    assert translated["object"] == "chat.completion"
    choice = translated["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] == "Calling."
    assert choice["message"]["reasoning_content"] == "deliberating"
    assert choice["message"]["tool_calls"] == [
        {"id": "toolu_1", "type": "function", "function": {"name": "f", "arguments": '{"x":1}'}}
    ]
    # Mirror of the Anthropic mapping: OpenAI's prompt total is inclusive of cache reads.
    assert translated["usage"] == {
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "total_tokens": 120,
        "prompt_tokens_details": {"cached_tokens": 30},
    }


# --------------------------------------------------------------------------------------
# OpenAI Responses pairs
# --------------------------------------------------------------------------------------


def test_responses_request_and_reply_map_onto_chat_completions() -> None:
    pair = translator(RESPONSES, CHAT)
    upstream = pair.translate_request(
        {
            "model": "glm-5.3",
            "instructions": "Be brief.",
            "max_output_tokens": 256,
            "reasoning": {"effort": "low"},
            "input": [
                {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
                {"type": "function_call", "call_id": "c1", "name": "f", "arguments": '{"x":1}'},
                {"type": "function_call_output", "call_id": "c1", "output": "done"},
            ],
            "tools": [{"type": "function", "name": "f", "parameters": {"type": "object"}}],
        }
    )

    assert upstream["max_tokens"] == 256
    assert upstream["reasoning_effort"] == "low"
    assert upstream["messages"][0] == {"role": "system", "content": "Be brief."}
    assert upstream["messages"][1] == {"role": "user", "content": "hi"}
    assert upstream["messages"][3] == {"role": "tool", "tool_call_id": "c1", "content": "done"}
    assert upstream["tools"][0]["function"]["name"] == "f"

    translated = pair.translate_response(
        {
            "id": "chatcmpl-5",
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {"content": "partial"},
                }
            ],
            "usage": {"prompt_tokens": 9, "completion_tokens": 4},
        },
        CONTEXT,
    )

    assert translated["object"] == "response"
    assert translated["id"] == "resp_chatcmpl-5"
    assert translated["status"] == "incomplete"
    assert translated["output"][0]["content"] == [
        {"type": "output_text", "text": "partial", "annotations": []}
    ]
    assert translated["usage"] == {"input_tokens": 9, "output_tokens": 4, "total_tokens": 13}


def test_anthropic_from_responses_composes_through_chat_completions() -> None:
    """The chained pair must produce the same shape as a hand-written one would."""
    translated = translator(ANTHROPIC, RESPONSES).translate_response(
        {
            "id": "resp_1",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "hello"}],
                },
                {
                    "type": "function_call",
                    "call_id": "c1",
                    "name": "f",
                    "arguments": '{"x":1}',
                },
            ],
            "usage": {"input_tokens": 11, "output_tokens": 3},
        },
        CONTEXT,
    )

    assert translated["type"] == "message"
    assert translated["model"] == "glm-5.3"
    assert translated["stop_reason"] == "tool_use"
    assert translated["content"] == [
        {"type": "text", "text": "hello"},
        {"type": "tool_use", "id": "c1", "name": "f", "input": {"x": 1}},
    ]
    assert translated["usage"] == {"input_tokens": 11, "output_tokens": 3}
