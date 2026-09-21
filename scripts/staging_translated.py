import json

from gateway.protocols import Capability, ClientProtocol

CHAT = ClientProtocol.OPENAI_CHAT_COMPLETIONS
RESPONSES = ClientProtocol.OPENAI_RESPONSES
ANTHROPIC = ClientProtocol.ANTHROPIC_MESSAGES
PROTOCOLS = (CHAT, RESPONSES, ANTHROPIC)
PATHS = {CHAT: "/v1/chat/completions", RESPONSES: "/v1/responses", ANTHROPIC: "/v1/messages"}
CAPABILITIES = frozenset({Capability.STREAMING, Capability.TOOL_CALLING, Capability.REASONING})
PROMPT = "qualification-translated-v1: compare the two canned city results."
THOUGHT = "Synthetic reasoning: compare the two fixed results."
FINAL = "Paris: 18 C; Montréal: 24 C."
CALLS = [("call_paris", "lookup_weather", {"city": "Paris"}),
         ("call_montreal", "lookup_weather", {"city": "Montréal"})]
RESULTS = [{"temperature_c": 18}, {"temperature_c": 24}]
DELAY = .03
SCHEMA = {"type": "object", "properties": {"city": {"type": "string"}},
          "required": ["city"], "additionalProperties": False}
SCENARIOS = tuple((p, stream) for p in PROTOCOLS for stream in (False, True))


def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def tools(protocol):
    definition = {"name": "lookup_weather", "description": "Return a canned city result."}
    definition["input_schema" if protocol == ANTHROPIC else "parameters"] = SCHEMA
    if protocol == CHAT:
        return [{"type": "function", "function": definition}]
    return [definition if protocol == ANTHROPIC else {"type": "function", **definition}]


def request_body(protocol, stream=False):
    body = {"model": "qualification-model", "stream": stream, "tools": tools(protocol)}
    prompt = {"role": "user", "content": PROMPT}
    if protocol == RESPONSES:
        body.update(input=[prompt], max_output_tokens=256, reasoning={"effort": "medium"})
    elif protocol == ANTHROPIC:
        body.update(messages=[prompt], max_tokens=256,
                    thinking={"type": "enabled", "budget_tokens": 128})
    else:
        body.update(messages=[prompt], max_tokens=256, reasoning_effort="medium")
        if stream:
            body["stream_options"] = {"include_usage": True}
    return body


def decoded_calls(protocol, calls):
    if protocol == ANTHROPIC:
        return [(c["id"], c["name"], c["input"]) for c in calls]
    if protocol == RESPONSES:
        return [(c["call_id"], c["name"], json.loads(c["arguments"])) for c in calls]
    return [(c["id"], c["function"]["name"], json.loads(c["function"]["arguments"]))
            for c in calls]


def continuation(protocol, body, assistant):
    outputs = [(call[0], dump(result)) for call, result in zip(CALLS, RESULTS, strict=True)]
    if protocol == RESPONSES:
        return {**body, "input": [*body["input"], *assistant, *[
            {"type": "function_call_output", "call_id": i, "output": value}
            for i, value in outputs]]}
    if protocol == ANTHROPIC:
        messages = [{"role": "assistant", "content": assistant}, {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": i, "content": value} for i, value in outputs]}]
    else:
        messages = [assistant, *[{"role": "tool", "tool_call_id": i, "content": value}
                                for i, value in outputs]]
    return {**body, "messages": [*body["messages"], *messages]}


def synthetic_response(body):
    messages = body["messages"]
    assert messages[0] == {"role": "user", "content": PROMPT}, "synthetic prompt mismatch"
    assert body["tools"] == tools(CHAT), "synthetic tools mismatch"
    assert body.get("reasoning_effort"), "reasoning opt-in lost"
    assert body["max_tokens"] == 256, "token bound lost"
    second = len(messages) > 1
    if second:
        assert len(messages) == 4, "continuation must contain exactly two results"
        assert messages[1]["role"] == "assistant", "missing assistant turn"
        assert decoded_calls(CHAT, messages[1]["tool_calls"]) == CALLS, "tool calls mismatch"
        assert all(m["role"] == "tool" for m in messages[2:]), "tool result role mismatch"
        assert [(m["tool_call_id"], json.loads(m["content"])) for m in messages[2:]] == [
            (c[0], r) for c, r in zip(CALLS, RESULTS, strict=True)], "canned results mismatch"
    calls = [{"id": i, "type": "function", "function": {"name": n, "arguments": dump(a)}}
             for i, n, a in CALLS]
    message = {"role": "assistant", "content": FINAL if second else None,
               "reasoning_content": THOUGHT}
    if not second:
        message["tool_calls"] = calls
    return {"id": "chatcmpl-synthetic", "object": "chat.completion", "created": 1,
            "model": "synthetic", "choices": [{"index": 0, "message": message,
                "finish_reason": "stop" if second else "tool_calls"}],
            "usage": {"prompt_tokens": 32, "completion_tokens": 24, "total_tokens": 56}}


def synthetic_frames(full):
    choice = full["choices"][0]
    message = choice["message"]

    def chunk(delta, finish=None):
        return ("data: " + dump({"id": full["id"], "object": "chat.completion.chunk",
            "model": "synthetic", "choices": [
                {"index": 0, "delta": delta, "finish_reason": finish}]}) + "\n\n").encode()

    yield chunk({"role": "assistant"})
    for part in (THOUGHT[:20], THOUGHT[20:]):
        yield chunk({"reasoning_content": part})
    for index, call in enumerate(message.get("tool_calls", [])):
        yield chunk({"tool_calls": [{**call, "index": index,
            "function": {**call["function"], "arguments": ""}}]})
        arguments = call["function"]["arguments"]
        for part in (arguments[:10], arguments[10:]):
            yield chunk({"tool_calls": [{"index": index, "function": {"arguments": part}}]})
    if message["content"]:
        for part in (FINAL[:12], FINAL[12:]):
            yield chunk({"content": part})
    yield chunk({}, choice["finish_reason"])
    yield ("data: " + dump({"id": full["id"], "choices": [], "usage": full["usage"]})
           + "\n\n").encode()
    yield b"data: [DONE]\n\n"


def validate_json(protocol, body, second=False):
    if protocol == CHAT:
        assert body["object"] == "chat.completion"
        assert len(body["choices"]) == 1
        choice = body["choices"][0]
        assert choice["finish_reason"] == ("stop" if second else "tool_calls")
        assistant = choice["message"]
        thought = assistant.get("reasoning_content", "")
        calls, text = assistant.get("tool_calls", []), assistant.get("content") or ""
    elif protocol == ANTHROPIC:
        assert body["type"] == "message"
        assert body["stop_reason"] == ("end_turn" if second else "tool_use")
        assistant = body["content"]
        thought = "".join(b["thinking"] for b in assistant if b["type"] == "thinking")
        calls = [b for b in assistant if b["type"] == "tool_use"]
        text = "".join(b["text"] for b in assistant if b["type"] == "text")
    else:
        assert body["object"] == "response" and body["status"] == "completed"
        assistant = body["output"]
        thought = "".join(s["text"] for b in assistant if b["type"] == "reasoning"
                          for s in b["summary"])
        calls = [b for b in assistant if b["type"] == "function_call"]
        text = "".join(c["text"] for b in assistant if b["type"] == "message"
                       for c in b["content"])
    assert thought == THOUGHT, "reasoning mismatch"
    assert decoded_calls(protocol, calls) == ([] if second else CALLS), "tool IDs/args mismatch"
    assert text == (FINAL if second else ""), "final answer mismatch"
    return assistant


def validate_sse(protocol, response, second=False):
    assert response.headers["content-type"].startswith("text/event-stream")
    events = []
    for frame in response.text.replace("\r\n", "\n").split("\n\n"):
        data = "\n".join(line[5:].lstrip() for line in frame.splitlines()
                         if line.startswith("data:"))
        if not data:
            continue
        event = data if data == "[DONE]" else json.loads(data)
        assert not isinstance(event, dict) or "error" not in event, "SSE error"
        if protocol != CHAT:
            assert [line[6:].strip() for line in frame.splitlines()
                    if line.startswith("event:")] == [event["type"]]
        events.append(event)
    thought, text, calls, finishes = "", "", {}, []
    if protocol == CHAT:
        assert events[-1] == "[DONE]" and events.count("[DONE]") == 1
        for event in events[:-1]:
            for choice in event["choices"]:
                if choice.get("finish_reason"):
                    finishes.append(choice["finish_reason"])
                delta = choice["delta"]
                thought += delta.get("reasoning_content", "")
                text += delta.get("content", "")
                for call in delta.get("tool_calls", []):
                    slot = calls.setdefault(call["index"], {"id": "", "type": "function",
                        "function": {"name": "", "arguments": ""}})
                    slot["id"] += call.get("id", "")
                    for field in ("name", "arguments"):
                        slot["function"][field] += call.get("function", {}).get(field, "")
        assert finishes == ["stop" if second else "tool_calls"]
        assistant = {"role": "assistant", "content": text, "reasoning_content": thought}
        if calls:
            assistant["tool_calls"] = list(calls.values())
    elif protocol == ANTHROPIC:
        assert events[0]["type"] == "message_start" and events[-1]["type"] == "message_stop"
        assert sum(e["type"] == "message_stop" for e in events) == 1
        blocks, stopped = {}, set()
        for event in events:
            kind = event["type"]
            if kind == "content_block_start":
                assert event["index"] not in blocks and set(blocks) == stopped
                blocks[event["index"]] = {**event["content_block"], "arguments": ""}
            elif kind == "content_block_delta":
                index, delta = event["index"], event["delta"]
                assert index in blocks and index not in stopped
                thought += delta.get("thinking", "")
                text += delta.get("text", "")
                blocks[index]["arguments"] += delta.get("partial_json", "")
            elif kind == "content_block_stop":
                assert event["index"] in blocks and event["index"] not in stopped
                stopped.add(event["index"])
            elif kind == "message_delta":
                finishes.append(event["delta"]["stop_reason"])
        assert stopped == set(blocks)
        assert finishes == ["end_turn" if second else "tool_use"]
        calls = {i: {"type": "tool_use", "id": b["id"], "name": b["name"],
                     "input": json.loads(b["arguments"])}
                 for i, b in blocks.items() if b["type"] == "tool_use"}
        assistant = [{"type": "thinking", "thinking": thought, "signature": ""},
                     *calls.values()]
        if text:
            assistant.append({"type": "text", "text": text})
    else:
        assert events[0]["type"] == "response.created"
        assert events[-1]["type"] == "response.completed"
        assert sum(e["type"] == "response.completed" for e in events) == 1
        completed = events[-1]["response"]
        assistant = validate_json(protocol, completed, second)
        done = set()
        for event in events:
            kind = event["type"]
            if kind == "response.output_item.added" and event["item"]["type"] == "function_call":
                assert event["output_index"] not in calls
                calls[event["output_index"]] = dict(event["item"])
            elif kind == "response.function_call_arguments.delta":
                call = calls[event["output_index"]]
                assert call["id"] == event["item_id"]
                call["arguments"] += event["delta"]
            elif kind == "response.function_call_arguments.done":
                call = calls[event["output_index"]]
                assert call["id"] == event["item_id"] and call["arguments"] == event["arguments"]
                assert event["output_index"] not in done
                done.add(event["output_index"])
            elif kind == "response.reasoning_summary_text.delta":
                thought += event["delta"]
            elif kind == "response.output_text.delta":
                text += event["delta"]
            elif kind == "response.output_item.done":
                assert event["item"] == completed["output"][event["output_index"]]
        assert done == set(calls)
    assert thought == THOUGHT, "stream reasoning mismatch"
    assert text == (FINAL if second else ""), "stream final answer mismatch"
    assert decoded_calls(protocol, list(calls.values())) == ([] if second else CALLS), (
        "stream tool IDs/args mismatch")
    return assistant


def validate_response(protocol, response, stream, second=False):
    assert response.status_code == 200, f"unexpected HTTP {response.status_code}"
    return (validate_sse(protocol, response, second) if stream
            else validate_json(protocol, response.json(), second))
