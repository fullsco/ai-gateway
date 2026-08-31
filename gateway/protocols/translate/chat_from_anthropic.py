"""Client Chat Completions served by an upstream speaking Anthropic Messages.

The mirror of `anthropic_from_chat`: it lets OpenAI-protocol clients reach a model
whose only route is Anthropic-native.
"""

import json
import time
from typing import Any

from gateway.protocols.models import ClientProtocol
from gateway.protocols.translate._maps import (
    FINISH_REASON_FROM_STOP_REASON,
    content_parts_to_anthropic,
    thinking_from_reasoning_effort,
    tool_call_input,
    tool_choice_to_anthropic,
    tools_to_anthropic,
    usage_to_openai,
)
from gateway.protocols.translate.base import (
    ProtocolTranslator,
    SSEDecoder,
    SSEEvent,
    StreamTranslator,
    TranslationContext,
    format_sse,
)

_PASSTHROUGH_FIELDS = ("model", "temperature", "top_p", "stream")

# Anthropic rejects a request without max_tokens; Chat Completions treats it as
# optional and most clients omit it. A request that omitted it wanted the model's
# natural length, so the substitute has to be generous enough not to truncate.
DEFAULT_MAX_TOKENS = 8192


class ChatCompletionsFromAnthropic(ProtocolTranslator):
    client_protocol = ClientProtocol.OPENAI_CHAT_COMPLETIONS
    upstream_protocol = ClientProtocol.ANTHROPIC_MESSAGES

    def translate_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        upstream: dict[str, Any] = {
            key: payload[key] for key in _PASSTHROUGH_FIELDS if key in payload
        }
        system, messages = _messages_to_anthropic(payload.get("messages"))
        upstream["messages"] = messages
        if system:
            upstream["system"] = system
        limit = payload.get("max_completion_tokens")
        if not isinstance(limit, int) or isinstance(limit, bool):
            limit = payload.get("max_tokens")
        upstream["max_tokens"] = (
            limit if isinstance(limit, int) and not isinstance(limit, bool) else DEFAULT_MAX_TOKENS
        )
        stop = payload.get("stop")
        sequences = [stop] if isinstance(stop, str) else stop
        if isinstance(sequences, list):
            cleaned = [item for item in sequences if isinstance(item, str) and item]
            if cleaned:
                upstream["stop_sequences"] = cleaned
        tools = tools_to_anthropic(payload.get("tools"))
        if tools:
            upstream["tools"] = tools
            choice = tool_choice_to_anthropic(payload.get("tool_choice"))
            if choice is not None:
                upstream["tool_choice"] = choice
        thinking = thinking_from_reasoning_effort(payload.get("reasoning_effort"))
        if thinking is not None:
            upstream["thinking"] = thinking
        user = payload.get("user")
        if isinstance(user, str) and user:
            upstream["metadata"] = {"user_id": user}
        return upstream

    def translate_response(
        self, payload: dict[str, Any], context: TranslationContext
    ) -> dict[str, Any]:
        texts: list[str] = []
        reasoning: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        content = payload.get("content")
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text" and isinstance(block.get("text"), str):
                texts.append(block["text"])
            elif kind == "thinking" and isinstance(block.get("thinking"), str):
                reasoning.append(block["thinking"])
            elif kind == "tool_use":
                tool_calls.append(_tool_call(block, len(tool_calls)))
        message: dict[str, Any] = {"role": "assistant", "content": "\n".join(texts)}
        if reasoning:
            message["reasoning_content"] = "\n".join(reasoning)
        if tool_calls:
            message["tool_calls"] = tool_calls
        stop_reason = str(payload.get("stop_reason") or "end_turn")
        return {
            "id": _completion_id(payload.get("id")),
            "object": "chat.completion",
            "created": int(time.time()),
            "model": context.requested_model,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": FINISH_REASON_FROM_STOP_REASON.get(stop_reason, "stop"),
                }
            ],
            "usage": usage_to_openai(payload.get("usage")) or {},
        }

    def stream(self, context: TranslationContext) -> StreamTranslator:
        return _AnthropicToChatStream(context)


def _messages_to_anthropic(source: Any) -> tuple[str, list[dict[str, Any]]]:
    """Chat Completions messages -> (Anthropic system prompt, Anthropic messages).

    Two shape changes matter: system is a top-level field for Anthropic rather than a
    message, and a `role: tool` message becomes a `tool_result` block inside the next
    user turn. Consecutive tool messages collapse into one user turn, which is what
    Anthropic requires when a model called several tools at once.
    """
    system_parts: list[str] = []
    messages: list[dict[str, Any]] = []
    pending_results: list[dict[str, Any]] = []

    def flush_results() -> None:
        if pending_results:
            messages.append({"role": "user", "content": list(pending_results)})
            pending_results.clear()

    for message in source if isinstance(source, list) else []:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        content = message.get("content")
        if role == "system" or role == "developer":
            flush_results()
            text = content if isinstance(content, str) else _plain_text(content)
            if text:
                system_parts.append(text)
        elif role == "tool":
            pending_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": str(message.get("tool_call_id") or ""),
                    "content": content if isinstance(content, str) else _plain_text(content),
                }
            )
        elif role == "assistant":
            flush_results()
            blocks = content_parts_to_anthropic(content)
            blocks.extend(_calls(message.get("tool_calls")))
            if blocks:
                messages.append({"role": "assistant", "content": blocks})
        else:
            flush_results()
            blocks = content_parts_to_anthropic(content)
            if blocks:
                messages.append({"role": "user", "content": blocks})
    flush_results()
    return "\n\n".join(system_parts), messages


def _calls(tool_calls: Any) -> list[dict[str, Any]]:
    if not isinstance(tool_calls, list):
        return []
    blocks: list[dict[str, Any]] = []
    for position, call in enumerate(tool_calls):
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


def _plain_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        part["text"]
        for part in content
        if isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"]
    )


def _tool_call(block: dict[str, Any], position: int) -> dict[str, Any]:
    payload = block.get("input")
    return {
        "id": str(block.get("id") or f"call_{position}"),
        "type": "function",
        "function": {
            "name": str(block.get("name") or ""),
            "arguments": json.dumps(
                payload if isinstance(payload, dict) else {}, separators=(",", ":")
            ),
        },
    }


def _completion_id(upstream_id: Any) -> str:
    if isinstance(upstream_id, str) and upstream_id:
        return upstream_id if upstream_id.startswith("chatcmpl") else f"chatcmpl-{upstream_id}"
    return "chatcmpl-gateway-translated"


class _AnthropicToChatStream(StreamTranslator):
    """Flattens Anthropic's framed event sequence into Chat Completions chunks.

    Anthropic carries structure in the event names and a block index; Chat Completions
    carries it in a flat delta plus a tool-call index that must be contiguous and
    zero-based, independent of which content block the tool happened to occupy.
    """

    def __init__(self, context: TranslationContext) -> None:
        self._context = context
        self._decoder = SSEDecoder()
        self._completion_id = "chatcmpl-gateway-translated"
        self._created = int(time.time())
        self._finish_reason: str | None = None
        self._usage: dict[str, Any] | None = None
        self._blocks: dict[int, str] = {}
        self._tool_slots: dict[int, int] = {}
        self._role_sent = False
        self._done = False

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
        payload = event.json()
        if payload is None:
            return b""
        kind = str(payload.get("type") or event.name or "")
        if kind == "error":
            self._done = True
            error = payload.get("error")
            error = error if isinstance(error, dict) else {}
            return format_sse(None, {"error": {
                "type": str(error.get("type") or "api_error"),
                "message": str(error.get("message") or "The upstream provider failed."),
            }})
        if kind == "message_start":
            return self._message_start(payload)
        if kind == "content_block_start":
            return self._block_start(payload)
        if kind == "content_block_delta":
            return self._block_delta(payload)
        if kind == "message_delta":
            delta = payload.get("delta")
            if isinstance(delta, dict):
                stop_reason = str(delta.get("stop_reason") or "")
                if stop_reason:
                    self._finish_reason = FINISH_REASON_FROM_STOP_REASON.get(stop_reason, "stop")
            self._merge_usage(payload.get("usage"))
            return b""
        if kind == "message_stop":
            return self._close()
        # `ping` and `content_block_stop` have no Chat Completions counterpart: the
        # flat format has no notion of a block ending, and a chunk fabricated for one
        # would look like empty model output.
        return b""

    def _message_start(self, payload: dict[str, Any]) -> bytes:
        message = payload.get("message")
        message = message if isinstance(message, dict) else {}
        identifier = message.get("id")
        if isinstance(identifier, str) and identifier:
            self._completion_id = _completion_id(identifier)
        self._merge_usage(message.get("usage"))
        self._role_sent = True
        return self._chunk({"role": "assistant", "content": ""})

    def _block_start(self, payload: dict[str, Any]) -> bytes:
        index = payload.get("index")
        block = payload.get("content_block")
        block = block if isinstance(block, dict) else {}
        kind = str(block.get("type") or "")
        if not isinstance(index, int):
            return b""
        self._blocks[index] = kind
        if kind != "tool_use":
            return b""
        slot = len(self._tool_slots)
        self._tool_slots[index] = slot
        return self._chunk(
            {
                "tool_calls": [
                    {
                        "index": slot,
                        "id": str(block.get("id") or f"call_{slot}"),
                        "type": "function",
                        "function": {"name": str(block.get("name") or ""), "arguments": ""},
                    }
                ]
            }
        )

    def _block_delta(self, payload: dict[str, Any]) -> bytes:
        delta = payload.get("delta")
        delta = delta if isinstance(delta, dict) else {}
        index = payload.get("index")
        kind = str(delta.get("type") or "")
        if kind == "text_delta" and isinstance(delta.get("text"), str):
            return self._chunk({"content": delta["text"]})
        if kind == "thinking_delta" and isinstance(delta.get("thinking"), str):
            return self._chunk({"reasoning_content": delta["thinking"]})
        if kind == "input_json_delta" and isinstance(index, int):
            slot = self._tool_slots.get(index)
            if slot is None:
                return b""
            partial = delta.get("partial_json")
            if not isinstance(partial, str) or not partial:
                return b""
            return self._chunk(
                {"tool_calls": [{"index": slot, "function": {"arguments": partial}}]}
            )
        # `signature_delta` carries an Anthropic replay signature that means nothing
        # to an OpenAI client and would corrupt the reasoning text if concatenated.
        return b""

    def _merge_usage(self, usage: Any) -> None:
        if not isinstance(usage, dict):
            return
        merged = dict(self._usage or {})
        merged.update({key: value for key, value in usage.items() if value is not None})
        self._usage = merged

    def _chunk(self, delta: dict[str, Any], finish_reason: str | None = None) -> bytes:
        return format_sse(
            None,
            {
                "id": self._completion_id,
                "object": "chat.completion.chunk",
                "created": self._created,
                "model": self._context.requested_model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
            },
        )

    def _close(self) -> bytes:
        if self._done:
            return b""
        self._done = True
        out = bytearray()
        if not self._role_sent:
            out += self._chunk({"role": "assistant", "content": ""})
        out += self._chunk({}, finish_reason=self._finish_reason or "stop")
        usage = usage_to_openai(self._usage)
        if usage:
            # Chat Completions reports usage in a trailing chunk with no choices,
            # which is where clients that set stream_options.include_usage read it.
            out += format_sse(
                None,
                {
                    "id": self._completion_id,
                    "object": "chat.completion.chunk",
                    "created": self._created,
                    "model": self._context.requested_model,
                    "choices": [],
                    "usage": usage,
                },
            )
        out += format_sse(None, "[DONE]")
        return bytes(out)
