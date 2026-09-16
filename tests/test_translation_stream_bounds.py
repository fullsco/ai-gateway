"""Retained stream state is bounded across events, not just within an SSE frame."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.api.executor import _stream_response
from gateway.protocols import ClientProtocol
from gateway.protocols.event_buffer import EventTooLarge
from gateway.protocols.translate import TranslationContext, get_translator
from gateway.protocols.translate.base import SSEDecoder, format_sse
from gateway.protocols.translate.responses_stream import _StreamRetention

CHAT = ClientProtocol.OPENAI_CHAT_COMPLETIONS
ANTHROPIC = ClientProtocol.ANTHROPIC_MESSAGES
RESPONSES = ClientProtocol.OPENAI_RESPONSES
DONE = format_sse(None, "[DONE]")
LIMIT_ERROR = "Upstream stream retained state exceeds the supported limit"


def stream_for(protocol):
    return get_translator(protocol, CHAT).stream(TranslationContext("model", True))


def chunk(**delta):
    return format_sse(None, {"choices": [{"delta": delta}]})


def tool(index=0, *, arguments="", **fields):
    return chunk(tool_calls=[{"index": index, "function": {"arguments": arguments}, **fields}])


def events(wire):
    # Decode independently of the translator and its decoder.
    return [json.loads(line[6:]) for line in wire.decode().splitlines()
            if line.startswith("data: ") and line != "data: [DONE]"]


def assert_failed(stream):
    assert stream._retained.bytes <= _StreamRetention.MAX_BYTES
    for action in (lambda: stream.feed(tool(arguments="x")),
                   lambda: stream.feed(DONE), stream.finish):
        with pytest.raises(EventTooLarge, match=LIMIT_ERROR):
            action()


@pytest.mark.parametrize("protocol", [ANTHROPIC, RESPONSES])
def test_actual_translator_many_small_tool_chunks_hit_default_aggregate_cap(protocol):
    stream = stream_for(protocol)
    # Every event is tiny compared with the decoder limit; only retained state overflows.
    wire = tool(arguments="é" * 512)
    assert len(wire) < SSEDecoder.MAX_EVENT_BYTES
    count = 0
    with pytest.raises(EventTooLarge, match=LIMIT_ERROR):
        for _ in range(1100):
            stream.feed(wire)
            count += 1
    assert 1000 < count < 1024
    assert_failed(stream)


@pytest.mark.parametrize("field", ["content", "reasoning_content"])
def test_responses_many_text_or_reasoning_chunks_hit_default_cap(field):
    stream = stream_for(RESPONSES)
    count = 0
    with pytest.raises(EventTooLarge, match=LIMIT_ERROR):
        for _ in range(1100):
            stream.feed(chunk(**{field: "é" * 512}))
            count += 1
    assert 1000 < count < 1024
    assert_failed(stream)


@pytest.mark.parametrize("field", ["id", "name"])
def test_anthropic_fragmented_tool_metadata_is_bounded(field):
    stream = stream_for(ANTHROPIC)
    delta = {"index": 0, field: "x" * 1024} if field == "id" else {
        "index": 0, "function": {"name": "x" * 1024}}
    with pytest.raises(EventTooLarge, match=LIMIT_ERROR):
        for _ in range(1100):
            stream.feed(chunk(tool_calls=[delta]))
    assert_failed(stream)


@pytest.mark.parametrize("protocol", [ANTHROPIC, RESPONSES])
def test_empty_calls_hit_default_call_count_cap(protocol):
    stream = stream_for(protocol)
    for index in range(_StreamRetention.MAX_TOOL_CALLS):
        stream.feed(tool(index))
    with pytest.raises(EventTooLarge, match=LIMIT_ERROR):
        stream.feed(tool(_StreamRetention.MAX_TOOL_CALLS))
    assert stream._retained.calls == 1024
    assert_failed(stream)


def test_anthropic_tiny_fragments_hit_container_cap_before_byte_cap():
    stream = stream_for(ANTHROPIC)
    for index in range(_StreamRetention.MAX_ARGUMENT_FRAGMENTS):
        stream.feed(tool(index % 2, arguments="x"))
    with pytest.raises(EventTooLarge, match=LIMIT_ERROR):
        stream.feed(tool(arguments="x"))
    assert sum(len(b["arguments"]) for b in stream._tool_blocks.values()) == 16384
    assert stream._retained.bytes < _StreamRetention.MAX_BYTES
    assert_failed(stream)


def test_anthropic_does_not_charge_text_or_reasoning_that_is_not_retained():
    stream = stream_for(ANTHROPIC)
    wire = chunk(content="x" * 1024, reasoning_content="y" * 1024)
    for _ in range(1100):
        output = events(stream.feed(wire))
        assert [e["delta"] for e in output if e["type"] == "content_block_delta"] == [
            {"type": "thinking_delta", "thinking": "y" * 1024},
            {"type": "text_delta", "text": "x" * 1024},
        ]
    assert stream._retained.bytes < 100
    assert events(stream.finish())[-1]["type"] == "message_stop"


def test_responses_shares_one_budget_for_text_reasoning_tools_and_metadata(monkeypatch):
    monkeypatch.setattr(_StreamRetention, "MAX_BYTES", 1024)
    stream = stream_for(RESPONSES)
    stream.feed(chunk(content="x" * 200, reasoning_content="y" * 200))
    stream.feed(tool(0, arguments="a" * 200, id="call_a"))
    stream.feed(tool(1, arguments="b" * 200, id="call_b"))
    remaining = 1024 - stream._retained.bytes
    stream.feed(chunk(content="z" * remaining))
    assert stream._retained.bytes == 1024
    # Exact cap is legal; one additional UTF-8 byte is not.
    with pytest.raises(EventTooLarge, match=LIMIT_ERROR):
        stream.feed(chunk(reasoning_content="!"))
    assert_failed(stream)


@pytest.mark.parametrize("protocol", [ANTHROPIC, RESPONSES])
def test_calls_share_byte_budget_and_utf8_is_counted(monkeypatch, protocol):
    monkeypatch.setattr(_StreamRetention, "MAX_BYTES", 1024)
    stream = stream_for(protocol)
    stream.feed(tool(0, arguments="é" * 150))
    stream.feed(tool(1, arguments="é" * 150))
    remaining = 1024 - stream._retained.bytes
    stream.feed(tool(0, arguments="x" * remaining))
    assert stream._retained.bytes == 1024
    with pytest.raises(EventTooLarge, match=LIMIT_ERROR):
        stream.feed(tool(1, arguments="é"))
    assert_failed(stream)


@pytest.mark.parametrize("protocol", [ANTHROPIC, RESPONSES])
@pytest.mark.parametrize("metadata", ["id", "usage", "tool"])
def test_metadata_cannot_bypass_aggregate_budget(monkeypatch, protocol, metadata):
    monkeypatch.setattr(_StreamRetention, "MAX_BYTES", 1024)
    stream = stream_for(protocol)
    stream.feed(tool(arguments="x" * 600))
    payload = {"id": "m" * 600} if metadata == "id" else {
        "usage": {"extension": "m" * 600}}
    if metadata == "id" and protocol == RESPONSES:
        # Responses freezes its ID on the first event; put ID first instead.
        stream = stream_for(protocol)
        stream.feed(format_sse(None, payload))
        wire = tool(arguments="x" * 600)
    elif metadata == "tool":
        wire = chunk(tool_calls=[{"index": 1, "id": "m" * 600,
                                  "function": {"name": "n" * 100}}])
    else:
        wire = format_sse(None, payload)
    with pytest.raises(EventTooLarge, match=LIMIT_ERROR):
        stream.feed(wire)
    assert_failed(stream)


@pytest.mark.parametrize("protocol", [ANTHROPIC, RESPONSES])
def test_replaced_metadata_does_not_spend_budget_repeatedly(monkeypatch, protocol):
    monkeypatch.setattr(_StreamRetention, "MAX_BYTES", 1024)
    stream = stream_for(protocol)
    usage = format_sse(None, {"usage": {"prompt_tokens": 5, "completion_tokens": 7}})
    for _ in range(2000):
        stream.feed(usage)
    assert stream._retained.bytes < 100
    assert stream.finish()


@pytest.mark.parametrize("protocol", [ANTHROPIC, RESPONSES])
@pytest.mark.parametrize("key", [[], {}, "x" * 1000, -1, True, None])
def test_invalid_tool_indices_fail_deterministically(protocol, key):
    stream = stream_for(protocol)
    with pytest.raises(EventTooLarge, match="tool call index"):
        stream.feed(tool(key))
    with pytest.raises(EventTooLarge, match="tool call index"):
        stream.finish()


@pytest.mark.parametrize("protocol", [ANTHROPIC, RESPONSES])
def test_interleaved_calls_reasoning_and_text_preserved_at_exact_cap(monkeypatch, protocol):
    monkeypatch.setattr(_StreamRetention, "MAX_BYTES", 4096)
    stream = stream_for(protocol)
    out = bytearray()
    for index in (7, 2):
        out += stream.feed(chunk(tool_calls=[{"index": index, "id": f"call_{index}",
                                             "function": {"name": f"lookup_{index}"}}]))
    expected = {7: '{"city":"', 2: '{"city":"'}
    for index in (7, 2):
        out += stream.feed(tool(index, arguments=expected[index]))
    for _ in range(40):
        out += stream.feed(chunk(reasoning_content="hmm", content="yes"))
        for index, value in ((2, "é"), (7, "a")):
            out += stream.feed(tool(index, arguments=value))
            expected[index] += value
    for index in (7, 2):
        out += stream.feed(tool(index, arguments='"}'))
        expected[index] += '"}'
    # JSON permits trailing whitespace; fill precisely to the retained byte cap.
    padding = " " * (4096 - stream._retained.bytes)
    out += stream.feed(tool(7, arguments=padding))
    expected[7] += padding
    out += stream.feed(DONE)
    assert stream.finish() == b""
    parsed = events(out)
    if protocol == RESPONSES:
        assert [e["sequence_number"] for e in parsed] == list(range(len(parsed)))
        output = parsed[-1]["response"]["output"]
        calls = [item for item in output if item["type"] == "function_call"]
        assert [(c["call_id"], c["name"], c["arguments"]) for c in calls] == [
            (f"call_{i}", f"lookup_{i}", expected[i]) for i in (7, 2)]
        assert next(i for i in output if i["type"] == "reasoning")["summary"][0][
            "text"] == "hmm" * 40
        assert next(i for i in output if i["type"] == "message")["content"][0][
            "text"] == "yes" * 40
    else:
        blocks, values = [], {}
        active = None
        for event in parsed:
            if event["type"] == "content_block_start":
                assert active is None
                active = event["index"]
                blocks.append(event["content_block"])
                values[active] = ""
            elif event["type"] == "content_block_delta":
                assert event["index"] == active
                delta = event["delta"]
                values[active] += delta.get("text", delta.get("thinking", delta.get(
                    "partial_json", "")))
            elif event["type"] == "content_block_stop":
                assert event["index"] == active
                active = None
        assert active is None
        calls = [(block["id"], block["name"], values[i]) for i, block in enumerate(blocks)
                 if block["type"] == "tool_use"]
        assert calls == [(f"call_{i}", f"lookup_{i}", expected[i]) for i in (7, 2)]
        for kind, expected_text in (("text", "yes" * 40), ("thinking", "hmm" * 40)):
            assert "".join(values[i] for i, b in enumerate(blocks)
                           if b["type"] == kind) == expected_text
        assert parsed[-1]["type"] == "message_stop"
    retained = stream._retained.bytes
    for _ in range(100):
        assert stream.feed(tool(999, arguments="unexpected")) == b""
    assert stream._retained.bytes == retained


@pytest.mark.parametrize("protocol", [ANTHROPIC, RESPONSES])
@pytest.mark.parametrize("flush", [False, True])
def test_overflow_during_feed_or_finish_uses_executor_terminal_error(monkeypatch, protocol, flush):
    import asyncio

    monkeypatch.setattr(_StreamRetention, "MAX_BYTES", 1024)
    stream = stream_for(protocol)
    finalizer = SimpleNamespace(
        usage=Mock(), completion=SimpleNamespace(feed=Mock(), successful=True),
        client_protocol=protocol, provider_id="provider", provider_model_id="model",
        attempt_number=1, finish=AsyncMock(),
    )

    async def remaining():
        for _ in range(50):
            yield tool(arguments="x" * 10)
        wire = tool(arguments="y" * 600)
        yield wire.rstrip(b"\n") if flush else wire

    async def run():
        return b"".join([part async for part in _stream_response(
            chunk(content="started"), remaining(), finalizer, translator=stream)])

    parsed = events(asyncio.run(run()))
    assert parsed[-1]["type"] == "error"
    assert parsed[-1]["error"]["type"] == "upstream_truncated"
    assert not any(e["type"] in ("message_stop", "response.completed") for e in parsed)
    finalizer.finish.assert_awaited_once()
    assert finalizer.finish.call_args.kwargs["error"] is not None
    assert_failed(stream)
