"""Client Chat Completions served by an upstream speaking OpenAI Responses."""

import time
from typing import Any

from gateway.protocols.models import ClientProtocol
from gateway.protocols.translate.base import (
    ProtocolTranslator,
    SSEDecoder,
    SSEEvent,
    StreamTranslator,
    TranslationContext,
    format_sse,
)

_PASSTHROUGH_FIELDS = ("model", "temperature", "top_p", "stream")

_FINISH_REASON_FROM_STATUS = {
    "completed": "stop",
    "incomplete": "length",
    "failed": "stop",
    "cancelled": "stop",
}


class ChatCompletionsFromResponses(ProtocolTranslator):
    client_protocol = ClientProtocol.OPENAI_CHAT_COMPLETIONS
    upstream_protocol = ClientProtocol.OPENAI_RESPONSES

    def translate_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        upstream: dict[str, Any] = {
            key: payload[key] for key in _PASSTHROUGH_FIELDS if key in payload
        }
        instructions, items = _messages_to_input(payload.get("messages"))
        upstream["input"] = items
        if instructions:
            upstream["instructions"] = instructions
        limit = payload.get("max_completion_tokens")
        if not isinstance(limit, int) or isinstance(limit, bool):
            limit = payload.get("max_tokens")
        if isinstance(limit, int) and not isinstance(limit, bool):
            upstream["max_output_tokens"] = limit
        tools = _tools_to_responses(payload.get("tools"))
        if tools:
            upstream["tools"] = tools
            choice = _tool_choice_to_responses(payload.get("tool_choice"))
            if choice is not None:
                upstream["tool_choice"] = choice
        effort = payload.get("reasoning_effort")
        if isinstance(effort, str) and effort:
            upstream["reasoning"] = {"effort": effort}
        response_format = payload.get("response_format")
        if isinstance(response_format, dict) and response_format.get("type") != "text":
            upstream["text"] = {"format": response_format}
        return upstream

    def translate_response(
        self, payload: dict[str, Any], context: TranslationContext
    ) -> dict[str, Any]:
        texts: list[str] = []
        reasoning: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        output = payload.get("output")
        for item in output if isinstance(output, list) else []:
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            if kind == "message":
                texts.append(_item_text(item.get("content")))
            elif kind == "reasoning":
                reasoning.append(_summary_text(item))
            elif kind == "function_call":
                tool_calls.append(
                    {
                        "id": str(item.get("call_id") or item.get("id") or ""),
                        "type": "function",
                        "function": {
                            "name": str(item.get("name") or ""),
                            "arguments": str(item.get("arguments") or "{}"),
                        },
                    }
                )
        message: dict[str, Any] = {"role": "assistant", "content": "".join(texts)}
        joined_reasoning = "\n".join(part for part in reasoning if part)
        if joined_reasoning:
            message["reasoning_content"] = joined_reasoning
        if tool_calls:
            message["tool_calls"] = tool_calls
        status = str(payload.get("status") or "completed")
        finish_reason = (
            "tool_calls" if tool_calls else _FINISH_REASON_FROM_STATUS.get(status, "stop")
        )
        return {
            "id": _completion_id(payload.get("id")),
            "object": "chat.completion",
            "created": int(time.time()),
            "model": context.requested_model,
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
            "usage": _usage_to_chat(payload.get("usage")),
        }

    def stream(self, context: TranslationContext) -> StreamTranslator:
        return _ResponsesToChatStream(context)


def _messages_to_input(source: Any) -> tuple[str, list[dict[str, Any]]]:
    instructions: list[str] = []
    items: list[dict[str, Any]] = []
    for message in source if isinstance(source, list) else []:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        content = message.get("content")
        if role in {"system", "developer"}:
            text = content if isinstance(content, str) else _plain_text(content)
            if text:
                instructions.append(text)
            continue
        if role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": str(message.get("tool_call_id") or ""),
                    "output": content if isinstance(content, str) else _plain_text(content),
                }
            )
            continue
        if role == "assistant":
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                function = call.get("function")
                function = function if isinstance(function, dict) else {}
                items.append(
                    {
                        "type": "function_call",
                        "call_id": str(call.get("id") or ""),
                        "name": str(function.get("name") or ""),
                        "arguments": str(function.get("arguments") or "{}"),
                    }
                )
        parts = _content_to_input(content, assistant=role == "assistant")
        if parts:
            items.append({"role": str(role or "user"), "content": parts})
    return "\n\n".join(instructions), items


def _content_to_input(content: Any, *, assistant: bool) -> list[dict[str, Any]]:
    text_type = "output_text" if assistant else "input_text"
    if isinstance(content, str):
        return [{"type": text_type, "text": content}] if content else []
    if not isinstance(content, list):
        return []
    parts: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind == "text" and isinstance(part.get("text"), str) and part["text"]:
            parts.append({"type": text_type, "text": part["text"]})
        elif kind == "image_url":
            url = part.get("image_url")
            url = url.get("url") if isinstance(url, dict) else url
            if isinstance(url, str) and url:
                parts.append({"type": "input_image", "image_url": url})
    return parts


def _tools_to_responses(tools: Any) -> list[dict[str, Any]] | None:
    if not isinstance(tools, list):
        return None
    converted: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        source = function if isinstance(function, dict) else tool
        name = source.get("name")
        if not isinstance(name, str) or not name:
            continue
        entry: dict[str, Any] = {"type": "function", "name": name}
        description = source.get("description")
        if isinstance(description, str) and description:
            entry["description"] = description
        parameters = source.get("parameters")
        entry["parameters"] = (
            parameters if isinstance(parameters, dict) else {"type": "object", "properties": {}}
        )
        converted.append(entry)
    return converted or None


def _tool_choice_to_responses(choice: Any) -> Any:
    if isinstance(choice, str):
        return choice if choice in {"auto", "none", "required"} else None
    if isinstance(choice, dict):
        function = choice.get("function")
        name = function.get("name") if isinstance(function, dict) else choice.get("name")
        if isinstance(name, str) and name:
            return {"type": "function", "name": name}
    return None


def _item_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        part["text"]
        for part in content
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    )


def _summary_text(item: dict[str, Any]) -> str:
    summary = item.get("summary")
    if isinstance(summary, str):
        return summary
    if not isinstance(summary, list):
        return ""
    return "\n".join(
        part["text"]
        for part in summary
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    )


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


def _usage_to_chat(usage: Any) -> dict[str, Any]:
    if not isinstance(usage, dict):
        return {}
    prompt = _int(usage.get("input_tokens")) or 0
    completion = _int(usage.get("output_tokens")) or 0
    details = usage.get("input_tokens_details")
    cached = _int(details.get("cached_tokens")) if isinstance(details, dict) else None
    translated: dict[str, Any] = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": _int(usage.get("total_tokens")) or (prompt + completion),
    }
    if cached:
        translated["prompt_tokens_details"] = {"cached_tokens": cached}
    return translated


def _completion_id(upstream_id: Any) -> str:
    if isinstance(upstream_id, str) and upstream_id:
        return upstream_id if upstream_id.startswith("chatcmpl") else f"chatcmpl-{upstream_id}"
    return "chatcmpl-gateway-translated"


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


class _ResponsesToChatStream(StreamTranslator):
    """Flattens Responses events into Chat Completions chunks.

    Responses numbers its output items; Chat Completions needs a contiguous
    zero-based tool-call index, so function-call items are re-slotted in the order
    they appear rather than by their output index.
    """

    def __init__(self, context: TranslationContext) -> None:
        self._context = context
        self._decoder = SSEDecoder()
        self._completion_id = "chatcmpl-gateway-translated"
        self._created = int(time.time())
        self._role_sent = False
        self._done = False
        self._finish_reason: str | None = None
        self._usage: dict[str, Any] = {}
        self._tool_slots: dict[Any, int] = {}
        self._saw_tool_call = False

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
        if kind == "response.created":
            response = payload.get("response")
            identifier = response.get("id") if isinstance(response, dict) else None
            if isinstance(identifier, str) and identifier:
                self._completion_id = _completion_id(identifier)
            return self._ensure_role()
        if kind == "response.output_text.delta":
            delta = payload.get("delta")
            if isinstance(delta, str) and delta:
                return self._ensure_role() + self._chunk({"content": delta})
            return b""
        if kind in {"response.reasoning_summary_text.delta", "response.reasoning_text.delta"}:
            delta = payload.get("delta")
            if isinstance(delta, str) and delta:
                return self._ensure_role() + self._chunk({"reasoning_content": delta})
            return b""
        if kind == "response.output_item.added":
            return self._item_added(payload)
        if kind == "response.function_call_arguments.delta":
            return self._arguments_delta(payload)
        if kind in {"response.completed", "response.incomplete", "response.failed"}:
            response = payload.get("response")
            if isinstance(response, dict):
                usage = response.get("usage")
                if isinstance(usage, dict):
                    self._usage = usage
                status = str(response.get("status") or "completed")
                self._finish_reason = _FINISH_REASON_FROM_STATUS.get(status, "stop")
            return self._close()
        if kind == "error":
            self._done = True
            message = payload.get("message") or payload.get("error")
            return format_sse(
                None,
                {
                    "error": {
                        "type": str(payload.get("code") or "api_error"),
                        "message": str(message) if message else "The upstream provider failed.",
                    }
                },
            )
        return b""

    def _item_added(self, payload: dict[str, Any]) -> bytes:
        item = payload.get("item")
        item = item if isinstance(item, dict) else {}
        if item.get("type") != "function_call":
            return b""
        key = payload.get("output_index", item.get("id"))
        slot = len(self._tool_slots)
        self._tool_slots[key] = slot
        self._saw_tool_call = True
        return self._ensure_role() + self._chunk(
            {
                "tool_calls": [
                    {
                        "index": slot,
                        "id": str(item.get("call_id") or item.get("id") or f"call_{slot}"),
                        "type": "function",
                        "function": {"name": str(item.get("name") or ""), "arguments": ""},
                    }
                ]
            }
        )

    def _arguments_delta(self, payload: dict[str, Any]) -> bytes:
        key = payload.get("output_index")
        slot = self._tool_slots.get(key)
        if slot is None:
            slot = self._tool_slots.get(payload.get("item_id"))
        if slot is None:
            return b""
        delta = payload.get("delta")
        if not isinstance(delta, str) or not delta:
            return b""
        return self._chunk({"tool_calls": [{"index": slot, "function": {"arguments": delta}}]})

    def _ensure_role(self) -> bytes:
        if self._role_sent:
            return b""
        self._role_sent = True
        return self._chunk({"role": "assistant", "content": ""})

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
        out = bytearray(self._ensure_role())
        finish_reason = "tool_calls" if self._saw_tool_call else (self._finish_reason or "stop")
        out += self._chunk({}, finish_reason=finish_reason)
        usage = _usage_to_chat(self._usage)
        if usage:
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
