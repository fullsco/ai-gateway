"""Offline client->gateway->TCP upstream matrix, including incremental SSE wire decoding."""

import asyncio
import json
from contextlib import asynccontextmanager
from itertools import product

import httpx
import pytest

from gateway.app import create_app
from gateway.auth import ClientPermissions, GatewayClient, InMemoryGatewayKeyStore
from gateway.config import Settings
from gateway.models import CanonicalModel, ModelRegistry, ProviderModel
from gateway.protocols import Capability, ClientProtocol
from gateway.protocols.translate import get_translator
from gateway.providers import Credential, ProviderConfig
from gateway.providers.anthropic import AnthropicCompatibleAdapter
from gateway.providers.openai import OpenAICompatibleAdapter
from gateway.providers.translating import TranslatingAdapter
from gateway.routing import CredentialState, ProviderState, RoutingEngine
from gateway.runtime import GatewayRuntime
from gateway.security.gateway_keys import GatewayKeyHasher

CHAT = ClientProtocol.OPENAI_CHAT_COMPLETIONS
ANTHROPIC = ClientProtocol.ANTHROPIC_MESSAGES
RESPONSES = ClientProtocol.OPENAI_RESPONSES
PROTOCOLS = (CHAT, ANTHROPIC, RESPONSES)
LABELS = {CHAT: "chat", ANTHROPIC: "anthropic", RESPONSES: "responses"}
PATHS = {CHAT: "/v1/chat/completions", ANTHROPIC: "/v1/messages", RESPONSES: "/v1/responses"}
PAIRS = [pytest.param(c, u, id=f"{LABELS[c]}-to-{LABELS[u]}")
         for c, u in product(PROTOCOLS, repeat=2)]
CAPABILITIES = frozenset({Capability.STREAMING, Capability.TOOL_CALLING, Capability.REASONING})
CALLS = [("call_paris", "lookup_weather", {"city": "Paris"}),
         ("call_montreal", "lookup_weather", {"city": "Montréal"})]
RESULTS = [{"temperature_c": 18}, {"temperature_c": 24}]
PROMPT = "Compare the weather, then refresh both lookups."
THOUGHT = "Compare Montréal with Paris."
SCHEMA = {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}


def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def tools(protocol):
    definition = {"name": "lookup_weather", "description": "Look up weather."}
    definition["input_schema" if protocol == ANTHROPIC else "parameters"] = SCHEMA
    if protocol == CHAT:
        return [{"type": "function", "function": definition}]
    return [definition if protocol == ANTHROPIC else {"type": "function", **definition}]


def wire_calls(protocol):
    if protocol == ANTHROPIC:
        return [{"type": "tool_use", "id": i, "name": n, "input": a} for i, n, a in CALLS]
    if protocol == RESPONSES:
        # Item identity must never replace invocation identity on either wire.
        return [{"type": "function_call", "id": f"fc_{index}", "call_id": i,
                 "name": n, "arguments": dump(a), "status": "completed"}
                for index, (i, n, a) in enumerate(CALLS)]
    return [{"id": i, "type": "function", "function": {"name": n, "arguments": dump(a)}}
            for i, n, a in CALLS]


def request_body(protocol, stream):
    body = {"model": "alias-x", "stream": stream, "tools": tools(protocol)}
    prompt = {"role": "user", "content": PROMPT}
    results = [(call[0], dump(value)) for call, value in zip(CALLS, RESULTS, strict=True)]
    if protocol == RESPONSES:
        body.update(max_output_tokens=4096, reasoning={"effort": "medium"}, input=[
            prompt, *wire_calls(protocol), *[
                {"type": "function_call_output", "call_id": i, "output": value}
                for i, value in results]])
    elif protocol == ANTHROPIC:
        body.update(max_tokens=4096, thinking={"type": "enabled", "budget_tokens": 2048},
                    messages=[prompt, {"role": "assistant", "content": wire_calls(protocol)},
                              {"role": "user", "content": [
                                  {"type": "tool_result", "tool_use_id": i, "content": value}
                                  for i, value in results]}])
    else:
        body.update(max_tokens=4096, reasoning_effort="medium", messages=[
            prompt, {"role": "assistant", "content": "", "tool_calls": wire_calls(protocol)},
            *[{"role": "tool", "tool_call_id": i, "content": value} for i, value in results]])
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


def assert_request(protocol, seen, stream):
    method, path, headers, body = seen
    assert (method, path) == ("POST", PATHS[protocol])
    auth = ("x-api-key", "loopback-key") if protocol == ANTHROPIC else (
        "authorization", "Bearer loopback-key")
    assert headers[auth[0]] == auth[1]
    assert body["model"] == "upstream-x"
    assert body["stream"] is stream
    assert body["tools"] == tools(protocol)
    assert body["max_output_tokens" if protocol == RESPONSES else "max_tokens"] == 4096
    if protocol == RESPONSES:
        items = body["input"]
        calls = [x for x in items if x.get("type") == "function_call"]
        outputs = [x for x in items if x.get("type") == "function_call_output"]
        results = [(x["call_id"], json.loads(x["output"])) for x in outputs]
        assert max(items.index(c) for c in calls) < min(items.index(x) for x in outputs)
    elif protocol == ANTHROPIC:
        calls, results = [], []
        for message in body["messages"]:
            content = message["content"]
            for block in content if isinstance(content, list) else []:
                if block["type"] == "tool_use":
                    assert message["role"] == "assistant" and not results
                    calls.append(block)
                elif block["type"] == "tool_result":
                    assert message["role"] == "user"
                    results.append((block["tool_use_id"], json.loads(block["content"])))
    else:
        messages = body["messages"]
        assistants = [x for x in messages if x.get("tool_calls")]
        assert len(assistants) == 1
        calls = assistants[0]["tool_calls"]
        outputs = messages[messages.index(assistants[0]) + 1:]
        assert all(x["role"] == "tool" for x in outputs)
        results = [(x["tool_call_id"], json.loads(x["content"])) for x in outputs]
    assert decoded_calls(protocol, calls) == CALLS
    assert results == [(c[0], value) for c, value in zip(CALLS, RESULTS, strict=True)]
    assert PROMPT in dump(body)


def usage(protocol):
    if protocol == ANTHROPIC:
        return {"input_tokens": 100, "output_tokens": 45, "cache_read_input_tokens": 20,
                "cache_creation_input_tokens": 0}
    prefix = "prompt" if protocol == CHAT else "input"
    return {f"{prefix}_tokens": 120,
            "completion_tokens" if protocol == CHAT else "output_tokens": 45,
            "total_tokens": 165, f"{prefix}_tokens_details": {"cached_tokens": 20}}


def provider_response(protocol):
    calls = wire_calls(protocol)
    if protocol == ANTHROPIC:
        return {"id": "msg_network", "type": "message", "role": "assistant",
                "model": "upstream-x", "stop_reason": "tool_use", "stop_sequence": None,
                "content": [{"type": "thinking", "thinking": THOUGHT, "signature": "sig"},
                            *calls], "usage": usage(protocol)}
    if protocol == RESPONSES:
        return {"id": "resp_network", "object": "response", "status": "completed",
                "model": "upstream-x", "output": [
                    {"id": "rs_network", "type": "reasoning", "summary": [
                        {"type": "summary_text", "text": THOUGHT}]}, *calls],
                "usage": usage(protocol)}
    return {"id": "chatcmpl-network", "object": "chat.completion", "created": 1,
            "model": "upstream-x", "choices": [{"index": 0, "finish_reason": "tool_calls",
                "message": {"role": "assistant", "content": None,
                            "reasoning_content": THOUGHT, "tool_calls": calls}}],
            "usage": usage(protocol)}


def provider_sse(protocol, interleaved=False):
    frames = []

    def emit(kind=None, **body):
        if kind:
            body = {"type": kind, **body}
        frames.append(((f"event: {kind}\n" if kind else "") + f"data: {dump(body)}\n\n").encode())

    def chat(delta, finish=None):
        emit(id="chatcmpl-network", object="chat.completion.chunk", model="upstream-x",
             choices=[{"index": 0, "delta": delta, "finish_reason": finish}])

    full = provider_response(protocol)
    if protocol == CHAT:
        chat({"role": "assistant"})
        for part in (THOUGHT[:12], THOUGHT[12:]):
            chat({"reasoning_content": part})
        def start_call(index, call):
            chat({"tool_calls": [{**call, "index": index,
                                  "function": {**call["function"], "arguments": ""}}]})

        def arguments(index, args):
            for part in (slice(None, 9), slice(9, None)):
                chat({"tool_calls": [{"index": index,
                                      "function": {"arguments": dump(args)[part]}}]})

        for index, call in enumerate(wire_calls(protocol)):
            start_call(index, call)
            if not interleaved:
                arguments(index, CALLS[index][2])
        if interleaved:
            for part in (slice(None, 9), slice(9, None)):
                for index, (_, _, args) in enumerate(CALLS):
                    chat({"tool_calls": [{"index": index,
                                          "function": {"arguments": dump(args)[part]}}]})
        chat({}, "tool_calls")
        emit(id="chatcmpl-network", choices=[], usage=usage(protocol))
        frames.append(b"data: [DONE]\n\n")
    elif protocol == ANTHROPIC:
        emit("message_start", message={**full, "content": [], "stop_reason": None,
                                       "usage": {**usage(protocol), "output_tokens": 0}})
        emit("content_block_start", index=0, content_block={"type": "thinking", "thinking": ""})
        for part in (THOUGHT[:12], THOUGHT[12:]):
            emit("content_block_delta", index=0, delta={"type": "thinking_delta", "thinking": part})
        emit("content_block_delta", index=0, delta={"type": "signature_delta", "signature": "sig"})
        emit("content_block_stop", index=0)
        for index, call in enumerate(wire_calls(protocol), 1):
            emit("content_block_start", index=index, content_block={**call, "input": {}})
            for part in (slice(None, 9), slice(9, None)):
                emit("content_block_delta", index=index,
                     delta={"type": "input_json_delta", "partial_json": dump(call["input"])[part]})
            emit("content_block_stop", index=index)
        emit("message_delta", delta={"stop_reason": "tool_use", "stop_sequence": None},
             usage={"output_tokens": 45})
        emit("message_stop")
    else:
        emit("response.created", response={**full, "status": "in_progress", "output": []})
        reasoning = full["output"][0]
        emit("response.output_item.added", output_index=0, item={**reasoning, "summary": []})
        emit("response.reasoning_summary_part.added", output_index=0, item_id=reasoning["id"],
             summary_index=0, part={"type": "summary_text", "text": ""})
        for part in (THOUGHT[:12], THOUGHT[12:]):
            emit("response.reasoning_summary_text.delta", output_index=0,
                 item_id=reasoning["id"], summary_index=0, delta=part)
        emit("response.reasoning_summary_text.done", output_index=0,
             item_id=reasoning["id"], summary_index=0, text=THOUGHT)
        emit("response.output_item.done", output_index=0, item=reasoning)
        for index, call in enumerate(wire_calls(protocol), 1):
            emit("response.output_item.added", output_index=index,
                 item={**call, "arguments": "", "status": "in_progress"})
            for part in (slice(None, 9), slice(9, None)):
                emit("response.function_call_arguments.delta", output_index=index,
                     item_id=call["id"], delta=call["arguments"][part])
            emit("response.function_call_arguments.done", output_index=index,
                 item_id=call["id"], arguments=call["arguments"])
            emit("response.output_item.done", output_index=index, item=call)
        emit("response.completed", response=full)
    return b"".join(frames)


@asynccontextmanager
async def loopback_upstream(protocol, stream, interleaved=False):
    seen, errors, tasks = [], [], set()

    async def serve(reader, writer):
        tasks.add(asyncio.current_task())
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            first, *lines = head.decode().split("\r\n")
            method, path, _ = first.split()
            headers = {name.lower(): value for name, value in (
                line.split(": ", 1) for line in lines if line)}
            payload = await asyncio.wait_for(reader.readexactly(int(headers["content-length"])), 5)
            seen.append((method, path, headers, json.loads(payload)))
            data = provider_sse(protocol, interleaved) if stream else (
                dump(provider_response(protocol)).encode())
            content_type = "text/event-stream" if stream else "application/json"
            writer.write((f"HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\n"
                          "Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n").encode())
            # Force splits inside JSON, SSE delimiters, and multibyte UTF-8, with drains
            # between HTTP chunks. No assumption that TCP preserves write boundaries.
            cuts = sorted({0, len(data), *range(17, len(data), 97),
                           *(i + 1 for i, byte in enumerate(data) if byte == 0xC3)})
            for start, end in zip(cuts[:-1], cuts[1:], strict=True):
                chunk = data[start:end]
                writer.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                await writer.drain()
                await asyncio.sleep(0.001)
            writer.write(b"0\r\n\r\n")
            await writer.drain()
        except Exception as exc:
            errors.append(exc)
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}", seen
    finally:
        server.close()
        await server.wait_closed()
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks), 10)
        assert not errors, errors


def make_runtime(client_protocol, upstream_protocol, base_url, http):
    hasher = GatewayKeyHasher(b"p" * 32)
    issued = hasher.issue(key_id="key-1", client_id="client-1")
    client = GatewayClient("client-1", "test", ClientPermissions(frozenset({client_protocol})))
    mapping = ProviderModel("mapping", "model-x", "provider-a", "upstream-x", upstream_protocol,
                            CAPABILITIES, serves_protocols=frozenset({client_protocol}))
    registry = ModelRegistry([CanonicalModel("model-x", frozenset({"alias-x"}), CAPABILITIES)],
                             [mapping])
    adapter_type = AnthropicCompatibleAdapter if upstream_protocol == ANTHROPIC \
        else OpenAICompatibleAdapter
    adapter = adapter_type(ProviderConfig(id="provider-a", name="Loopback", base_url=base_url,
                                         protocol=upstream_protocol, capabilities=CAPABILITIES))
    translator = get_translator(client_protocol, upstream_protocol)
    assert client_protocol == upstream_protocol or translator is not None
    runtime = GatewayRuntime(
        key_store=InMemoryGatewayKeyStore([issued.record], [client]), key_hasher=hasher,
        model_registry=registry, routing_engine=RoutingEngine(registry),
        provider_states=(ProviderState("provider-a"),),
        credential_states=(CredentialState("credential-a", "provider-a"),),
        provider_model_adapters={"mapping": adapter},
        translating_adapters={("mapping", client_protocol): TranslatingAdapter(adapter, translator)}
        if translator else {},
        credentials={"credential-a": Credential(id="credential-a", secret="loopback-key")},
        http_client=http)
    return runtime, issued.plaintext


async def exercise(client_protocol, upstream_protocol, stream, interleaved=False, reasoning=True):
    async with loopback_upstream(upstream_protocol, stream, interleaved) as (url, seen), \
            httpx.AsyncClient(trust_env=False, timeout=5) as upstream:
        runtime, key = make_runtime(client_protocol, upstream_protocol, url, upstream)
        app = create_app(Settings(_env_file=None, database_url=None, environment="test",
                                  health_probe_enabled=False, alert_monitor_enabled=False,
                                  credential_usage_poll_enabled=False,
                                  failover_enabled=False), runtime)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                    base_url="http://gateway.test", trust_env=False) as client:
            body = request_body(client_protocol, stream)
            if not reasoning:
                for field in ("reasoning", "thinking", "reasoning_effort"):
                    body.pop(field, None)
            response = await asyncio.wait_for(client.post(
                PATHS[client_protocol],
                headers={"Authorization": f"Bearer {key}", "x-api-key": key},
                json=body), 10)
        assert response.status_code == 200, response.text
        assert len(seen) == 1, "Exactly one real TCP upstream dispatch required"
        assert_request(upstream_protocol, seen[0], stream)
    return response


def assert_usage(protocol, actual):
    for key, value in usage(protocol).items():
        if key == "cache_creation_input_tokens":
            assert actual.get(key, 0) == value
        else:
            assert actual[key] == value


def assert_nonstream(protocol, body):
    assert_usage(protocol, body["usage"])
    if protocol == CHAT:
        assert body["object"] == "chat.completion" and len(body["choices"]) == 1
        choice = body["choices"][0]
        assert choice["finish_reason"] == "tool_calls"
        assert decoded_calls(protocol, choice["message"]["tool_calls"]) == CALLS
        return choice["message"].get("reasoning_content", "")
    if protocol == ANTHROPIC:
        assert body["type"] == "message" and body["stop_reason"] == "tool_use"
        calls = [x for x in body["content"] if x["type"] == "tool_use"]
        assert decoded_calls(protocol, calls) == CALLS
        return "".join(x["thinking"] for x in body["content"] if x["type"] == "thinking")
    assert body["object"] == "response" and body["status"] == "completed"
    calls = [x for x in body["output"] if x["type"] == "function_call"]
    assert decoded_calls(protocol, calls) == CALLS
    return "".join(s["text"] for x in body["output"] if x["type"] == "reasoning"
                   for s in x["summary"])


def assert_stream(protocol, response):
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text.endswith("\n\n")
    if protocol != ANTHROPIC:
        assert "signature" not in response.text
    events = []
    for frame in response.text.split("\n\n"):
        if not frame.strip():
            continue
        data = "\n".join(line[5:].lstrip() for line in frame.splitlines()
                         if line.startswith("data:"))
        event = data if data == "[DONE]" else json.loads(data)
        assert not isinstance(event, dict) or "error" not in event
        if protocol != CHAT:
            names = [line[6:].strip() for line in frame.splitlines() if line.startswith("event:")]
            assert names == [event["type"]], "SSE event name must match the payload type"
        events.append(event)
    calls, thought = {}, ""
    if protocol == CHAT:
        assert events[-1] == "[DONE]" and events.count("[DONE]") == 1
        finishes = []
        usages = []
        for event in events[:-1]:
            if event.get("usage"):
                usages.append(event["usage"])
            for choice in event["choices"]:
                if choice.get("finish_reason"):
                    finishes.append(choice["finish_reason"])
                delta = choice["delta"]
                thought += delta.get("reasoning_content", "")
                for call in delta.get("tool_calls", []):
                    slot = calls.setdefault(call["index"], {"id": "", "function": {
                        "name": "", "arguments": ""}})
                    slot["id"] += call.get("id", "")
                    for field in ("name", "arguments"):
                        slot["function"][field] += call.get("function", {}).get(field, "")
        assert finishes == ["tool_calls"] and len(usages) == 1
        assert_usage(protocol, usages[0])
    elif protocol == ANTHROPIC:
        assert events[0]["type"] == "message_start"
        assert [e["type"] for e in events].count("message_stop") == 1
        assert events[-1]["type"] == "message_stop"
        blocks, stopped = {}, []
        actual_usage = dict(events[0]["message"]["usage"])
        finishes = []
        for event in events:
            kind = event["type"]
            if kind == "content_block_start":
                index = event["index"]
                assert sorted(stopped) == sorted(blocks)
                assert index not in blocks
                blocks[index] = {**event["content_block"], "arguments": ""}
            elif kind == "content_block_delta":
                assert event["index"] in blocks and event["index"] not in stopped
                delta = event["delta"]
                thought += delta.get("thinking", "")
                blocks[event["index"]]["arguments"] += delta.get("partial_json", "")
            elif kind == "content_block_stop":
                stopped.append(event["index"])
            elif kind == "message_delta":
                finishes.append(event["delta"]["stop_reason"])
                actual_usage.update(event.get("usage", {}))
        assert sorted(stopped) == sorted(blocks) and len(stopped) == len(set(stopped))
        assert finishes == ["tool_use"]
        assert_usage(protocol, actual_usage)
        assert all(b["arguments"] for b in blocks.values() if b["type"] == "tool_use"), blocks
        calls = {i: {**b, "input": json.loads(b["arguments"])}
                 for i, b in blocks.items() if b["type"] == "tool_use"}
    else:
        assert events[0]["type"] == "response.created"
        assert [e["type"] for e in events].count("response.completed") == 1
        assert events[-1]["type"] == "response.completed"
        completed = events[-1]["response"]
        done = []
        for event in events:
            kind = event["type"]
            if kind == "response.output_item.done":
                assert event["item"] == completed["output"][event["output_index"]]
            if kind == "response.output_item.added" and event["item"]["type"] == "function_call":
                assert event["output_index"] not in calls
                calls[event["output_index"]] = dict(event["item"])
            elif kind == "response.function_call_arguments.delta":
                call = calls[event["output_index"]]
                assert event["item_id"] == call["id"]
                call["arguments"] += event["delta"]
            elif kind == "response.function_call_arguments.done":
                call = calls[event["output_index"]]
                assert event["arguments"] == call["arguments"] and event["item_id"] == call["id"]
                done.append(event["output_index"])
            elif kind == "response.reasoning_summary_text.delta":
                thought += event["delta"]
            elif kind == "response.reasoning_summary_text.done":
                assert event["text"] == thought
        assert thought == assert_nonstream(protocol, completed)
        assert sorted(done) == sorted(calls) and len(done) == len(set(done))
    assert decoded_calls(protocol, list(calls.values())) == CALLS
    return thought


@pytest.mark.asyncio
@pytest.mark.parametrize("client_protocol,upstream_protocol", PAIRS)
@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
async def test_network_protocol_matrix(client_protocol, upstream_protocol, stream):
    response = await exercise(client_protocol, upstream_protocol, stream)
    if stream:
        assert_stream(client_protocol, response)
    else:
        assert assert_nonstream(client_protocol, response.json()) == THOUGHT


@pytest.mark.asyncio
@pytest.mark.parametrize("client_protocol,upstream_protocol", PAIRS)
@pytest.mark.parametrize("reasoning", [True, False], ids=["opt-in", "no-opt-in"])
async def test_network_reasoning_deltas(client_protocol, upstream_protocol, reasoning):
    response = await exercise(client_protocol, upstream_protocol, True, reasoning=reasoning)
    withheld = not reasoning and client_protocol in (ANTHROPIC, RESPONSES) \
        and client_protocol != upstream_protocol
    assert assert_stream(client_protocol, response) == ("" if withheld else THOUGHT)


@pytest.mark.asyncio
async def test_network_interleaved_chat_tools_to_anthropic():
    response = await exercise(ANTHROPIC, CHAT, True, interleaved=True)
    assert assert_stream(ANTHROPIC, response) == THOUGHT
