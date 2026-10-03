"""Unit tests for the streaming half of the translation matrix.

The stream translators are the part most likely to break subtly: upstream chunks do not
align to SSE event boundaries, and Anthropic clients require a strict frame order that
Chat Completions never states. Each test therefore checks the frame sequence, not just
that bytes came out.
"""

import json

from tests.translation_support import (
    ANTHROPIC,
    CHAT,
    CONTEXT,
    RESPONSES,
    THINKING_CONTEXT,
    frame_names,
    parse_sse,
    translator,
)

THINKING = THINKING_CONTEXT


def chat_chunk(**delta: object) -> bytes:
    body = {
        "id": "chatcmpl-77",
        "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
    }
    return f"data: {json.dumps(body)}\n\n".encode()


def chat_finish(reason: str) -> bytes:
    body = {
        "id": "chatcmpl-77",
        "choices": [{"index": 0, "delta": {}, "finish_reason": reason}],
    }
    return f"data: {json.dumps(body)}\n\n".encode()


def chat_usage(prompt: int, completion: int) -> bytes:
    body = {
        "id": "chatcmpl-77",
        "choices": [],
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        },
    }
    return f"data: {json.dumps(body)}\n\n".encode()


DONE = b"data: [DONE]\n\n"


def drain(stream, chunks: list[bytes], *, size: int | None = None) -> bytes:
    """Feed the stream, optionally re-sliced to `size` bytes to break event boundaries."""
    payload = b"".join(chunks)
    pieces = (
        [payload[at : at + size] for at in range(0, len(payload), size)]
        if size is not None
        else chunks
    )
    out = bytearray()
    for piece in pieces:
        out += stream.feed(piece)
    out += stream.finish()
    return bytes(out)


# --------------------------------------------------------------------------------------
# Chat Completions upstream -> Anthropic client
# --------------------------------------------------------------------------------------

TEXT_STREAM = [
    chat_chunk(role="assistant"),
    chat_chunk(content="Hel"),
    chat_chunk(content="lo"),
    chat_finish("stop"),
    chat_usage(11, 3),
    DONE,
]


def test_a_chat_text_stream_is_reframed_as_anthropic_events() -> None:
    pair = translator(ANTHROPIC, CHAT)
    events = parse_sse(drain(pair.stream(CONTEXT), TEXT_STREAM))

    assert [name for name, _ in events] == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    start = events[0][1]["message"]
    # message_start has to be synthesised before the first delta arrives, carrying the
    # model the client asked for and zeroed usage that message_delta later supplies.
    assert start["id"] == "msg_chatcmpl-77"
    assert start["model"] == "glm-5.3"
    assert start["usage"] == {"input_tokens": 0, "output_tokens": 0}
    assert events[1][1] == {
        "type": "content_block_start",
        "index": 0,
        "content_block": {"type": "text", "text": ""},
    }
    assert [event[1]["delta"]["text"] for event in events[2:4]] == ["Hel", "lo"]
    assert all(event[1]["index"] == 0 for event in events[1:5])
    assert events[5][1]["delta"] == {"stop_reason": "end_turn", "stop_sequence": None}
    assert events[5][1]["usage"] == {"input_tokens": 11, "output_tokens": 3}


def test_chunks_that_straddle_event_boundaries_translate_identically() -> None:
    """Upstream chunk boundaries are arbitrary; the client's frames must not be.

    The same fact that forces `_ends_on_event_boundary` in the executor: emitting on a
    half-read frame would hand the client truncated JSON.
    """
    pair = translator(ANTHROPIC, CHAT)
    whole = drain(pair.stream(CONTEXT), TEXT_STREAM)

    for size in (1, 3, 17, 64, 4096):
        assert drain(pair.stream(CONTEXT), TEXT_STREAM, size=size) == whole, size


def test_a_chat_tool_call_stream_becomes_input_json_delta_frames() -> None:
    pair = translator(ANTHROPIC, CHAT)
    chunks = [
        chat_chunk(role="assistant"),
        chat_chunk(content="Checking."),
        chat_chunk(
            tool_calls=[
                {
                    "index": 0,
                    "id": "call_a",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": ""},
                }
            ]
        ),
        chat_chunk(tool_calls=[{"index": 0, "function": {"arguments": '{"city":'}}]),
        chat_chunk(tool_calls=[{"index": 0, "function": {"arguments": '"Paris"}'}}]),
        chat_finish("tool_calls"),
        DONE,
    ]
    events = parse_sse(drain(pair.stream(CONTEXT), chunks))

    assert [name for name, _ in events] == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        # The text block must be closed before the tool block opens: Anthropic has no
        # way to reopen a stopped block, and two blocks cannot share an index.
        "content_block_stop",
        "content_block_start",
        "content_block_delta",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    assert events[4][1]["index"] == 1
    assert events[4][1]["content_block"] == {
        "type": "tool_use",
        "id": "call_a",
        "name": "get_weather",
        "input": {},
    }
    partials = [event[1]["delta"]["partial_json"] for event in events[5:7]]
    assert partials == ['{"city":', '"Paris"}']
    assert json.loads("".join(partials)) == {"city": "Paris"}
    assert all(event[1]["index"] == 1 for event in events[4:8])
    assert events[8][1]["delta"]["stop_reason"] == "tool_use"


def test_upstream_reasoning_deltas_are_only_framed_when_thinking_was_requested() -> None:
    pair = translator(ANTHROPIC, CHAT)
    chunks = [chat_chunk(reasoning_content="hmm"), chat_chunk(content="yes"), chat_finish("stop")]

    withheld = parse_sse(drain(pair.stream(CONTEXT), chunks))
    surfaced = parse_sse(drain(pair.stream(THINKING), chunks))

    def block_types(events) -> list[str]:
        return [
            payload["content_block"]["type"]
            for name, payload in events
            if name == "content_block_start"
        ]

    assert block_types(withheld) == ["text"]
    assert block_types(surfaced) == ["thinking", "text"]
    assert surfaced[2][1]["delta"] == {"type": "thinking_delta", "thinking": "hmm"}


def test_a_stream_that_ends_without_done_still_closes_the_anthropic_sequence() -> None:
    """An Anthropic client waits for message_stop; without it the request hangs."""
    pair = translator(ANTHROPIC, CHAT)
    # No `[DONE]`, and the final frame has no trailing blank line either.
    partial = [chat_chunk(content="hi"), b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}']

    names = frame_names(drain(pair.stream(CONTEXT), partial))

    assert names[-3:] == ["content_block_stop", "message_delta", "message_stop"]


def test_a_midstream_upstream_error_reaches_the_client_as_an_error_event() -> None:
    """A 200 that fails halfway can no longer become a status code."""
    pair = translator(ANTHROPIC, CHAT)
    chunks = [
        chat_chunk(content="hi"),
        b'data: {"error":{"type":"rate_limit_error","message":"slow down"}}\n\n',
    ]

    events = parse_sse(drain(pair.stream(CONTEXT), chunks))

    assert events[-1][0] == "error"
    assert events[-1][1]["error"] == {"type": "rate_limit_error", "message": "slow down"}
    # Already terminated: no message_stop is fabricated after an error frame.
    assert "message_stop" not in [name for name, _ in events]


def test_sse_comments_and_pings_are_not_mistaken_for_data() -> None:
    pair = translator(ANTHROPIC, CHAT)
    chunks = [
        b": keepalive\n\n",
        chat_chunk(content="hi"),
        b": ping\n\n",
        chat_finish("stop"),
        DONE,
    ]

    events = parse_sse(drain(pair.stream(CONTEXT), chunks))

    assert [name for name, _ in events] == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]


# --------------------------------------------------------------------------------------
# Anthropic upstream -> Chat Completions client
# --------------------------------------------------------------------------------------


def anthropic_event(name: str, payload: dict[str, object]) -> bytes:
    return f"event: {name}\ndata: {json.dumps({'type': name, **payload})}\n\n".encode()


ANTHROPIC_STREAM = [
    anthropic_event(
        "message_start",
        {"message": {"id": "msg_1", "usage": {"input_tokens": 10, "output_tokens": 1}}},
    ),
    anthropic_event(
        "content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}
    ),
    anthropic_event(
        "content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": "Hi"}}
    ),
    anthropic_event("content_block_stop", {"index": 0}),
    anthropic_event(
        "message_delta",
        {
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 5},
        },
    ),
    anthropic_event("message_stop", {}),
]


def test_an_anthropic_stream_is_flattened_into_chat_completion_chunks() -> None:
    pair = translator(CHAT, ANTHROPIC)
    events = parse_sse(drain(pair.stream(CONTEXT), ANTHROPIC_STREAM))

    # Chat Completions has no event names, and the stream ends with the literal
    # sentinel rather than a framed terminator.
    assert all(name is None for name, _ in events)
    assert events[-1][1] == "[DONE]"
    deltas = [event[1]["choices"][0]["delta"] for event in events[:3]]
    assert deltas == [{"role": "assistant", "content": ""}, {"content": "Hi"}, {}]
    assert events[2][1]["choices"][0]["finish_reason"] == "stop"
    assert events[0][1]["id"] == "chatcmpl-msg_1"
    assert events[0][1]["model"] == "glm-5.3"
    # Usage arrives in a trailing chunk with no choices, which is where a client that
    # set stream_options.include_usage reads it. message_start's count is merged with
    # message_delta's, landing on the real total.
    assert events[3][1]["choices"] == []
    assert events[3][1]["usage"] == {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
    }


def test_anthropic_tool_blocks_are_renumbered_into_contiguous_chat_slots() -> None:
    """Chat Completions tool indices must be zero-based and contiguous.

    Anthropic's index counts content blocks, so a tool that follows text sits at 1 and
    a second tool at 2. Forwarding those as tool-call indices makes an OpenAI client
    accumulate arguments into slots that were never opened.
    """
    pair = translator(CHAT, ANTHROPIC)
    chunks = [
        anthropic_event("message_start", {"message": {"id": "msg_2"}}),
        anthropic_event(
            "content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}
        ),
        anthropic_event(
            "content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": "go"}}
        ),
        anthropic_event("content_block_stop", {"index": 0}),
        anthropic_event(
            "content_block_start",
            {"index": 1, "content_block": {"type": "tool_use", "id": "toolu_a", "name": "f"}},
        ),
        anthropic_event(
            "content_block_delta",
            {"index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"x":1}'}},
        ),
        anthropic_event("content_block_stop", {"index": 1}),
        anthropic_event(
            "content_block_start",
            {"index": 2, "content_block": {"type": "tool_use", "id": "toolu_b", "name": "g"}},
        ),
        anthropic_event(
            "content_block_delta",
            {"index": 2, "delta": {"type": "input_json_delta", "partial_json": "{}"}},
        ),
        anthropic_event("message_delta", {"delta": {"stop_reason": "tool_use"}}),
        anthropic_event("message_stop", {}),
    ]
    events = parse_sse(drain(pair.stream(CONTEXT), chunks))
    calls = [
        call
        for name, payload in events
        if isinstance(payload, dict)
        for choice in payload.get("choices", [])
        for call in choice.get("delta", {}).get("tool_calls", [])
    ]

    assert [call["index"] for call in calls] == [0, 0, 1, 1]
    assert calls[0]["id"] == "toolu_a"
    assert calls[1]["function"]["arguments"] == '{"x":1}'
    assert calls[2]["id"] == "toolu_b"
    assert events[-2][1]["choices"][0]["finish_reason"] == "tool_calls"


def test_anthropic_signature_deltas_are_not_concatenated_into_reasoning() -> None:
    """A replay signature means nothing to an OpenAI client and would corrupt the text."""
    pair = translator(CHAT, ANTHROPIC)
    chunks = [
        anthropic_event("message_start", {"message": {"id": "msg_3"}}),
        anthropic_event(
            "content_block_start",
            {"index": 0, "content_block": {"type": "thinking", "thinking": ""}},
        ),
        anthropic_event(
            "content_block_delta",
            {"index": 0, "delta": {"type": "thinking_delta", "thinking": "why"}},
        ),
        anthropic_event(
            "content_block_delta",
            {"index": 0, "delta": {"type": "signature_delta", "signature": "SIGBYTES"}},
        ),
        anthropic_event("ping", {}),
        anthropic_event("message_stop", {}),
    ]
    output = drain(pair.stream(CONTEXT), chunks)

    assert b"SIGBYTES" not in output
    reasoning = [
        payload["choices"][0]["delta"]["reasoning_content"]
        for _, payload in parse_sse(output)
        if isinstance(payload, dict)
        and payload.get("choices")
        and "reasoning_content" in payload["choices"][0]["delta"]
    ]
    assert reasoning == ["why"]


def test_a_chained_stream_reframes_through_the_intermediate_protocol() -> None:
    """Anthropic client, Responses upstream: two translators in series, one sequence out."""
    pair = translator(ANTHROPIC, RESPONSES)
    chunks = [
        b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta",'
        b'"delta":"hi"}\n\n',
        b'event: response.completed\ndata: {"type":"response.completed","response":'
        b'{"id":"resp_1","status":"completed","usage":{"input_tokens":7,"output_tokens":2}}}\n\n',
    ]

    events = parse_sse(drain(pair.stream(CONTEXT), chunks))
    names = [name for name, _ in events]

    assert names[0] == "message_start"
    assert names[-1] == "message_stop"
    assert "content_block_delta" in names
    texts = [
        payload["delta"]["text"]
        for name, payload in events
        if name == "content_block_delta" and payload["delta"]["type"] == "text_delta"
    ]
    assert "".join(texts) == "hi"
    delta = next(payload for name, payload in events if name == "message_delta")
    assert delta["usage"] == {"input_tokens": 7, "output_tokens": 2}
