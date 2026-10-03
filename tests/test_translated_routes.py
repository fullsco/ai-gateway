"""Integration tests for routes that answer a client protocol their upstream cannot.

These drive the real HTTP path -- route selection, adapter choice, streaming relay and
usage accounting -- against a fake upstream, because the risk this design carries is not
in the mapping tables but in the executor's split between the *client* protocol and the
*upstream* one. Getting that wrong reintroduces the silent zero-token bug the codebase
already fixed once, which is why usage is asserted on the streamed path here.
"""

import json
from decimal import Decimal

import httpx
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.config import Settings
from tests.test_failover_api import BytesStream
from tests.translation_support import (
    ANTHROPIC,
    CHAT,
    PricedPool,
    build_translating_runtime,
    parse_sse,
)

# --------------------------------------------------------------------------------------
# the adapter map
# --------------------------------------------------------------------------------------


def test_a_served_protocol_resolves_to_a_translating_adapter_and_a_native_one_does_not() -> None:
    """A native route must resolve to the same object it did before translation existed.

    That is what keeps today's production traffic on today's code path rather than
    routing it through a new wrapper on the strength of a config field.
    """
    runtime, _ = build_translating_runtime(
        lambda _: httpx.Response(200, json={}), native_route=True
    )

    native = runtime.adapter_for("native-mapping", ANTHROPIC)
    upstream_native = runtime.adapter_for("chat-mapping", CHAT)
    translated = runtime.adapter_for("chat-mapping", ANTHROPIC)

    assert native is runtime.provider_model_adapters["native-mapping"]
    assert upstream_native is runtime.provider_model_adapters["chat-mapping"]
    assert translated is not runtime.provider_model_adapters["chat-mapping"]
    assert translated.inner is runtime.provider_model_adapters["chat-mapping"]
    # Health probes speak to the upstream, so they must keep reaching the real adapter.
    assert runtime.provider_model_adapters["chat-mapping"].config.protocol is CHAT


# --------------------------------------------------------------------------------------
# non-streaming
# --------------------------------------------------------------------------------------


def test_messages_endpoint_reaches_a_chat_completions_upstream() -> None:
    seen: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "model": "upstream-chat-mapping",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "Hello from upstream."},
                    }
                ],
                "usage": {"prompt_tokens": 11, "completion_tokens": 4, "total_tokens": 15},
            },
        )

    runtime, key = build_translating_runtime(handler)
    app = create_app(Settings(environment="test", _env_file=None), runtime)

    with TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            headers={"x-api-key": key},
            json={
                "model": "glm-5.3",
                "max_tokens": 64,
                "system": "Be brief.",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

    assert response.status_code == 200, response.text
    path, upstream_body = seen[0]
    # The upstream is addressed on its own endpoint, with its own model id and its own
    # request shape -- the wrapped adapter still owns URL, auth and headers.
    assert path == "/v1/chat/completions"
    assert upstream_body["model"] == "upstream-chat-mapping"
    assert upstream_body["messages"][0] == {"role": "system", "content": "Be brief."}

    body = response.json()
    assert body["type"] == "message"
    assert body["role"] == "assistant"
    assert body["content"] == [{"type": "text", "text": "Hello from upstream."}]
    assert body["stop_reason"] == "end_turn"
    # The client sees the name it asked for, not the upstream model id.
    assert body["model"] == "glm-5.3"
    assert body["usage"] == {"input_tokens": 11, "output_tokens": 4}
    # The translated body is longer than the upstream one, so a relayed content-length
    # would truncate it.
    assert "content-length" not in {name.lower() for name in response.headers} or int(
        response.headers["content-length"]
    ) == len(response.content)


def test_a_tool_use_round_trip_survives_both_directions() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if len(seen) == 1:
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-2",
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "call_a",
                                        "type": "function",
                                        "function": {
                                            "name": "get_weather",
                                            "arguments": '{"city":"Paris"}',
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 9},
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-3",
                "choices": [{"finish_reason": "stop", "message": {"content": "18C in Paris."}}],
                "usage": {"prompt_tokens": 30, "completion_tokens": 5},
            },
        )

    runtime, key = build_translating_runtime(handler)
    app = create_app(Settings(environment="test", _env_file=None), runtime)
    tools = [
        {
            "name": "get_weather",
            "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}},
        }
    ]

    with TestClient(app) as client:
        first = client.post(
            "/v1/messages",
            headers={"x-api-key": key},
            json={
                "model": "glm-5.3",
                "max_tokens": 64,
                "tools": tools,
                "messages": [{"role": "user", "content": "weather in Paris?"}],
            },
        ).json()
        second = client.post(
            "/v1/messages",
            headers={"x-api-key": key},
            json={
                "model": "glm-5.3",
                "max_tokens": 64,
                "tools": tools,
                "messages": [
                    {"role": "user", "content": "weather in Paris?"},
                    {"role": "assistant", "content": first["content"]},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "call_a",
                                "content": "18C",
                            }
                        ],
                    },
                ],
            },
        ).json()

    assert seen[0]["tools"][0]["function"]["name"] == "get_weather"
    assert first["stop_reason"] == "tool_use"
    assert first["content"] == [
        {"type": "tool_use", "id": "call_a", "name": "get_weather", "input": {"city": "Paris"}}
    ]
    # The block the client just received maps back onto the assistant turn and its
    # result onto a tool message, in the order OpenAI requires.
    assert [message["role"] for message in seen[1]["messages"]] == ["user", "assistant", "tool"]
    assert seen[1]["messages"][1]["tool_calls"][0]["id"] == "call_a"
    assert seen[1]["messages"][2] == {"role": "tool", "tool_call_id": "call_a", "content": "18C"}
    assert second["content"] == [{"type": "text", "text": "18C in Paris."}]


def test_an_untranslatable_upstream_body_fails_over_instead_of_reaching_the_client() -> None:
    """A body that cannot be restated must not arrive as unparseable JSON."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"upstream plain text", headers={"content-type": "text/plain"}
        )

    runtime, key = build_translating_runtime(handler)
    app = create_app(Settings(environment="test", _env_file=None), runtime)

    with TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            headers={"x-api-key": key},
            json={"model": "glm-5.3", "max_tokens": 8, "messages": []},
        )

    assert response.status_code >= 500
    assert b"upstream plain text" not in response.content


# --------------------------------------------------------------------------------------
# streaming
# --------------------------------------------------------------------------------------

CHAT_STREAM = [
    b'data: {"id":"chatcmpl-9","choices":[{"index":0,"delta":{"role":"assistant"}}]}\n\n',
    b'data: {"id":"chatcmpl-9","choices":[{"index":0,"delta":{"content":"Hel"}}]}\n',
    # Deliberately split across the event boundary: an upstream chunk has no obligation
    # to end on one, and a translator that assumed otherwise would emit truncated JSON.
    b'\ndata: {"id":"chatcmpl-9","choices":[{"index":0,"delta":{"content":"lo"}}]}\n\n',
    b'data: {"id":"chatcmpl-9","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n',
    b'data: {"id":"chatcmpl-9","choices":[],'
    b'"usage":{"prompt_tokens":31,"completion_tokens":7,"total_tokens":38}}\n\n',
    b"data: [DONE]\n\n",
]


def stream_handler(request: httpx.Request) -> httpx.Response:
    assert json.loads(request.content)["stream"] is True, "stream must survive translation"
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        stream=BytesStream(list(CHAT_STREAM)),
    )


def test_a_chat_completions_stream_reaches_the_client_as_anthropic_events() -> None:
    runtime, key = build_translating_runtime(stream_handler)
    app = create_app(Settings(environment="test", _env_file=None), runtime)

    with TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            headers={"x-api-key": key},
            json={"model": "glm-5.3", "max_tokens": 64, "stream": True, "messages": []},
        )

    assert response.status_code == 200, response.text
    events = parse_sse(response.text)
    assert [name for name, _ in events] == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    assert events[0][1]["message"]["model"] == "glm-5.3"
    assert "".join(event[1]["delta"]["text"] for event in events[2:4]) == "Hello"
    assert events[5][1]["usage"] == {"input_tokens": 31, "output_tokens": 7}
    # No Chat Completions frame leaks through, including its terminator.
    assert "chat.completion.chunk" not in response.text
    assert "[DONE]" not in response.text


def test_usage_and_cost_are_recorded_from_upstream_bytes_on_a_translated_stream() -> None:
    """The regression this design most risks.

    Usage extraction is keyed on a protocol, and on a translated route the bytes it
    reads are the *upstream's*. Reading them as the client's protocol finds no
    recognisable usage object, and the request is silently recorded as having cost
    nothing -- which is how streamed Anthropic traffic was zeroed once before.
    """
    pool = PricedPool()
    runtime, key = build_translating_runtime(stream_handler)
    app = create_app(Settings(environment="test", _env_file=None), runtime, db_pool=pool)

    with TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            headers={"x-api-key": key},
            json={"model": "glm-5.3", "max_tokens": 64, "stream": True, "messages": []},
        )

    assert response.status_code == 200
    usage_args = next(args for query, args in pool.execute_calls if "usage_records" in query)
    # prompt_tokens/completion_tokens off the upstream Chat Completions frames, not the
    # Anthropic frames the client received.
    assert usage_args[2:6] == (31, 7, None, None)
    assert usage_args[6] == Decimal("31") / 1_000_000 + Decimal("14") / 1_000_000
    assert usage_args[7] == "USD"
    # Recorded against the protocol actually spoken upstream, so per-protocol reporting
    # keeps matching what the provider billed.
    assert usage_args[14] == "openai_chat_completions"
    assert usage_args[15] == "succeeded"


# --------------------------------------------------------------------------------------
# routing across native and translated routes
# --------------------------------------------------------------------------------------


def test_a_native_route_is_preferred_and_a_translated_one_catches_the_failover() -> None:
    """Both routes serve `/v1/messages`; only priority decides, translation does not."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        if request.url.host == "anthropic.example":
            return httpx.Response(503, json={"error": {"message": "unavailable"}})
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-4",
                "choices": [{"finish_reason": "stop", "message": {"content": "from the fallback"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            },
        )

    runtime, key = build_translating_runtime(handler, native_route=True)
    app = create_app(Settings(environment="test", _env_file=None), runtime)

    with TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            headers={"x-api-key": key},
            json={"model": "glm-5.3", "max_tokens": 32, "messages": []},
        )

    assert response.status_code == 200, response.text
    # Priority 10 before priority 20, and the translated route is reached only after the
    # native one fails.
    assert seen == ["anthropic.example", "chat.example"]
    assert response.json()["content"] == [{"type": "text", "text": "from the fallback"}]


def test_a_mapping_still_answers_its_own_upstream_protocol_natively() -> None:
    """Adding a served protocol must not take the original one away."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-5",
                "object": "chat.completion",
                "choices": [{"finish_reason": "stop", "message": {"content": "native"}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1},
            },
        )

    runtime, key = build_translating_runtime(handler)
    app = create_app(Settings(environment="test", _env_file=None), runtime)

    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"authorization": f"Bearer {key}"},
            json={"model": "glm-5.3", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert response.status_code == 200, response.text
    assert seen == ["/v1/chat/completions"]
    # Byte-relayed, not round-tripped through a translator: the upstream's own object
    # type and id reach the client unchanged.
    assert response.json()["object"] == "chat.completion"
    assert response.json()["id"] == "chatcmpl-5"


def test_a_protocol_no_route_serves_is_still_reported_as_unavailable() -> None:
    """Served protocols widen reachability; they must not make every model reachable."""
    runtime, key = build_translating_runtime(lambda _: httpx.Response(200, json={}))
    app = create_app(Settings(environment="test", _env_file=None), runtime)

    with TestClient(app) as client:
        response = client.post(
            "/v1/responses",
            headers={"authorization": f"Bearer {key}"},
            json={"model": "glm-5.3", "input": "hi"},
        )

    assert response.status_code in {403, 404}
