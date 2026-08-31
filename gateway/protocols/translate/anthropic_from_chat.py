"""Client Anthropic Messages served by an upstream speaking Chat Completions.

This is the direction that unblocks Anthropic-only clients -- Claude Code calls
`/v1/messages` and nothing else -- from the many OpenAI-compatible upstreams that have
no Anthropic endpoint at all.
"""

import json
from typing import Any

from gateway.protocols.models import ClientProtocol
from gateway.protocols.translate._maps import (
    STOP_REASON_FROM_FINISH_REASON,
    content_parts_to_anthropic,
    content_parts_to_openai,
    reasoning_effort_from_thinking,
    tool_call_input,
    tool_choice_to_openai,
    tools_to_openai,
    usage_to_anthropic,
)
from gateway.protocols.translate.base import (
    ProtocolTranslator,
    SSEDecoder,
    SSEEvent,
    StreamTranslator,
    TranslationContext,
    format_sse,
)

# Passed through untouched because both protocols spell them the same way and mean the
# same thing. `top_k` is absent on purpose: Chat Completions has no equivalent, and
# forwarding it makes strict upstreams reject the whole request.
_PASSTHROUGH_FIELDS = ("model", "temperature", "top_p", "stream")


class AnthropicFromChatCompletions(ProtocolTranslator):
    client_protocol = ClientProtocol.ANTHROPIC_MESSAGES
    upstream_protocol = ClientProtocol.OPENAI_CHAT_COMPLETIONS

    def translate_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        upstream: dict[str, Any] = {
            key: payload[key] for key in _PASSTHROUGH_FIELDS if key in payload
        }
        upstream["messages"] = _messages_to_openai(payload)
        max_tokens = payload.get("max_tokens")
        if isinstance(max_tokens, int) and not isinstance(max_tokens, bool):
            upstream["max_tokens"] = max_tokens
        stop = payload.get("stop_sequences")
        if isinstance(stop, list) and stop:
            upstream["stop"] = [item for item in stop if isinstance(item, str)]
        tools = tools_to_openai(payload.get("tools"))
        if tools:
            upstream["tools"] = tools
            choice = tool_choice_to_openai(payload.get("tool_choice"))
            if choice is not None:
                upstream["tool_choice"] = choice
        effort = reasoning_effort_from_thinking(payload.get("thinking"))
        if effort is not None:
            upstream["reasoning_effort"] = effort
        metadata = payload.get("metadata")
        if isinstance(metadata, dict) and isinstance(metadata.get("user_id"), str):
            upstream["user"] = metadata["user_id"]
        return upstream

    def translate_response(
        self, payload: dict[str, Any], context: TranslationContext
    ) -> dict[str, Any]:
        choice = _first_choice(payload)
        message = choice.get("message")
        message = message if isinstance(message, dict) else {}
        blocks: list[dict[str, Any]] = []
        reasoning = _reasoning_text(message)
        if reasoning and context.reasoning_requested:
            blocks.append({"type": "thinking", "thinking": reasoning, "signature": ""})
        blocks.extend(content_parts_to_anthropic(message.get("content")))
        for call in _tool_calls(message.get("tool_calls")):
            blocks.append(call)
        stop_reason = STOP_REASON_FROM_FINISH_REASON.get(
            str(choice.get("finish_reason")), "end_turn"
        )
        if any(block.get("type") == "tool_use" for block in blocks):
            # Some OpenAI-compatible upstreams report `stop` alongside tool calls. An
            # Anthropic client keys its agent loop on stop_reason, so reporting
            # end_turn here would silently drop the tool call it just received.
            stop_reason = "tool_use"
        return {
            "id": _message_id(payload.get("id")),
            "type": "message",
            "role": "assistant",
            "model": context.requested_model,
            "content": blocks,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": usage_to_anthropic(payload.get("usage")),
        }

    def stream(self, context: TranslationContext) -> StreamTranslator:
        return _ChatToAnthropicStream(context)


def _messages_to_openai(payload: dict[str, Any]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    system = _system_text(payload.get("system"))
    if system:
        messages.append({"role": "system", "content": system})
    source = payload.get("messages")
    for message in source if isinstance(source, list) else []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if message.get("role") == "assistant":
            messages.extend(_assistant_to_openai(content))
            continue
        body, tool_results = content_parts_to_openai(content)
        # OpenAI requires every tool message to follow the assistant turn that
        # requested it, so results are emitted before whatever else the same
        # Anthropic user turn carried.
        messages.extend(tool_results)
        if body is not None and body != "":
            messages.append({"role": "user", "content": body})
    return messages


def _system_text(system: Any) -> str:
    if isinstance(system, str):
        return system
    if not isinstance(system, list):
        return ""
    return "\n\n".join(
        block["text"]
        for block in system
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    )


def _assistant_to_openai(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"role": "assistant", "content": content}] if content else []
    if not isinstance(content, list):
        return []
    texts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text" and isinstance(block.get("text"), str) and block["text"]:
            texts.append(block["text"])
        elif kind == "tool_use":
            tool_calls.append(
                {
                    "id": str(block.get("id") or ""),
                    "type": "function",
                    "function": {
                        "name": str(block.get("name") or ""),
                        "arguments": json.dumps(
                            block.get("input") if isinstance(block.get("input"), dict) else {},
                            separators=(",", ":"),
                        ),
                    },
                }
            )
        # `thinking` blocks are dropped. Their signature is meaningless to another
        # provider, and replaying the reasoning text as ordinary assistant content
        # would present the model's private deliberation as something it said aloud.
    if not texts and not tool_calls:
        return []
    message: dict[str, Any] = {"role": "assistant", "content": "\n".join(texts)}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return [message]


def _first_choice(payload: dict[str, Any]) -> dict[str, Any]:
    choices = payload.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if isinstance(choice, dict):
                return choice
    return {}


def _reasoning_text(container: dict[str, Any]) -> str:
    for key in ("reasoning_content", "reasoning"):
        value = container.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _tool_calls(calls: Any) -> list[dict[str, Any]]:
    if not isinstance(calls, list):
        return []
    blocks: list[dict[str, Any]] = []
    for position, call in enumerate(calls):
        if not isinstance(call, dict):
            continue
        function = call.get("function")
        function = function if isinstance(function, dict) else {}
        blocks.append(
            {
                "type": "tool_use",
                "id": str(call.get("id") or f"toolu_{position}"),
                "name": str(function.get("name") or ""),
                "input": tool_call_input(function.get("arguments")),
            }
        )
    return blocks


def _message_id(upstream_id: Any) -> str:
    if isinstance(upstream_id, str) and upstream_id:
        return upstream_id if upstream_id.startswith("msg_") else f"msg_{upstream_id}"
    return "msg_gateway_translated"


class _ChatToAnthropicStream(StreamTranslator):
    """Re-frames flat Chat Completions deltas as Anthropic's event sequence.

    Chat Completions streams one `choices[].delta` per chunk and says nothing about
    structure. Anthropic clients require a strict frame order -- `message_start`, then
    `content_block_start`/`_delta`/`_stop` per block with a monotonic index, then
    `message_delta` carrying the stop reason and usage, then `message_stop` -- so the
    shape has to be invented here and held consistent across chunks.
    """

    def __init__(self, context: TranslationContext) -> None:
        self._context = context
        self._decoder = SSEDecoder()
        self._started = False
        self._stopped = False
        self._next_index = 0
        self._open: tuple[int, str] | None = None
        self._tool_blocks: dict[Any, int] = {}
        self._stop_reason: str | None = None
        self._saw_tool_use = False
        self._usage: dict[str, Any] = {}
        self._message_id = "msg_gateway_translated"

    def feed(self, chunk: bytes) -> bytes:
        out = bytearray()
        for event in self._decoder.feed(chunk):
            out += self._handle(event)
        return bytes(out)

    def finish(self) -> bytes:
        out = bytearray()
        for event in self._decoder.flush():
            out += self._handle(event)
        out += self._close()
        return bytes(out)

    def _handle(self, event: SSEEvent) -> bytes:
        if event.is_done:
            return self._close()
        payload = event.json()
        if payload is None:
            return b""
        if "choices" not in payload and isinstance(payload.get("error"), dict):
            # An upstream that reports a mid-stream failure inside a 200 body. The
            # client is already committed to a stream, so the only way to tell it
            # is an Anthropic error event.
            return self._error_event(payload["error"])
        identifier = payload.get("id")
        if isinstance(identifier, str) and identifier and not self._started:
            self._message_id = _message_id(identifier)
        usage = payload.get("usage")
        if isinstance(usage, dict):
            self._usage = usage
        out = bytearray()
        choice = _first_choice(payload)
        delta = choice.get("delta")
        delta = delta if isinstance(delta, dict) else {}
        reasoning = _reasoning_text(delta)
        if reasoning and self._context.reasoning_requested:
            out += self._text_delta("thinking", "thinking_delta", "thinking", reasoning)
        for text in _delta_text(delta.get("content")):
            out += self._text_delta("text", "text_delta", "text", text)
        out += self._tool_deltas(delta.get("tool_calls"))
        finish_reason = choice.get("finish_reason")
        if isinstance(finish_reason, str) and finish_reason:
            self._stop_reason = STOP_REASON_FROM_FINISH_REASON.get(finish_reason, "end_turn")
        return bytes(out)

    def _ensure_started(self) -> bytes:
        if self._started:
            return b""
        self._started = True
        return format_sse(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": self._message_id,
                    "type": "message",
                    "role": "assistant",
                    "model": self._context.requested_model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    # Chat Completions reports usage only at the end of a stream, so
                    # the real counts arrive in message_delta. Anthropic SDKs merge
                    # the two, which lands on the right total.
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            },
        )

    def _text_delta(self, block_type: str, delta_type: str, field: str, text: str) -> bytes:
        out = bytearray(self._ensure_started())
        out += self._open_block(block_type, {"type": block_type, field: ""})
        index = self._open[0] if self._open else 0
        out += format_sse(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": index,
                "delta": {"type": delta_type, field: text},
            },
        )
        return bytes(out)

    def _tool_deltas(self, calls: Any) -> bytes:
        if not isinstance(calls, list):
            return b""
        out = bytearray()
        for position, call in enumerate(calls):
            if not isinstance(call, dict):
                continue
            key = call.get("index", position)
            function = call.get("function")
            function = function if isinstance(function, dict) else {}
            if key not in self._tool_blocks:
                out += self._ensure_started()
                out += self._close_block()
                self._tool_blocks[key] = self._next_index
                self._saw_tool_use = True
                out += self._start_block(
                    "tool_use",
                    {
                        "type": "tool_use",
                        "id": str(call.get("id") or f"toolu_{key}"),
                        "name": str(function.get("name") or ""),
                        "input": {},
                    },
                )
            arguments = function.get("arguments")
            if not isinstance(arguments, str) or not arguments:
                continue
            index = self._tool_blocks[key]
            if self._open is None or self._open[0] != index:
                # Arguments for a block that is no longer open cannot be framed:
                # Anthropic has no way to reopen a stopped block. Every provider
                # observed streams a tool call's arguments contiguously, so this is
                # a guard against corruption rather than a path taken in practice.
                continue
            out += format_sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {"type": "input_json_delta", "partial_json": arguments},
                },
            )
        return bytes(out)

    def _open_block(self, block_type: str, block: dict[str, Any]) -> bytes:
        if self._open is not None and self._open[1] == block_type:
            return b""
        out = bytearray(self._close_block())
        out += self._start_block(block_type, block)
        return bytes(out)

    def _start_block(self, block_type: str, block: dict[str, Any]) -> bytes:
        index = self._next_index
        self._next_index += 1
        self._open = (index, block_type)
        return format_sse(
            "content_block_start",
            {"type": "content_block_start", "index": index, "content_block": block},
        )

    def _close_block(self) -> bytes:
        if self._open is None:
            return b""
        index = self._open[0]
        self._open = None
        return format_sse("content_block_stop", {"type": "content_block_stop", "index": index})

    def _close(self) -> bytes:
        if self._stopped:
            return b""
        self._stopped = True
        out = bytearray(self._ensure_started())
        out += self._close_block()
        stop_reason = "tool_use" if self._saw_tool_use else (self._stop_reason or "end_turn")
        out += format_sse(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                "usage": usage_to_anthropic(self._usage),
            },
        )
        out += format_sse("message_stop", {"type": "message_stop"})
        return bytes(out)

    def _error_event(self, error: dict[str, Any]) -> bytes:
        self._stopped = True
        message = error.get("message")
        return format_sse(
            "error",
            {
                "type": "error",
                "error": {
                    "type": str(error.get("type") or "api_error"),
                    "message": str(message) if message else "The upstream provider failed.",
                },
            },
        )


def _delta_text(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content] if content else []
    if not isinstance(content, list):
        return []
    texts = []
    for part in content:
        if isinstance(part, str) and part:
            texts.append(part)
        elif isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"]:
            texts.append(part["text"])
    return texts
