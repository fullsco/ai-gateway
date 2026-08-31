"""Client OpenAI Responses served by an upstream speaking Chat Completions.

Together with its mirror this closes the matrix: with Anthropic <-> Chat Completions
mapped by hand and Responses <-> Chat Completions mapped here, the two Anthropic <->
Responses directions are composed through Chat Completions rather than written twice.
"""

import json
import time
from typing import Any

from gateway.protocols.models import ClientProtocol
from gateway.protocols.translate._responses_shared import (
    STATUS_FROM_FINISH_REASON,
    joined_text,
    response_id,
    usage_to_responses,
)
from gateway.protocols.translate.base import (
    ProtocolTranslator,
    StreamTranslator,
    TranslationContext,
)
from gateway.protocols.translate.responses_stream import ChatToResponsesStream

_PASSTHROUGH_FIELDS = ("model", "temperature", "top_p", "stream")


class ResponsesFromChatCompletions(ProtocolTranslator):
    client_protocol = ClientProtocol.OPENAI_RESPONSES
    upstream_protocol = ClientProtocol.OPENAI_CHAT_COMPLETIONS

    def translate_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        upstream: dict[str, Any] = {
            key: payload[key] for key in _PASSTHROUGH_FIELDS if key in payload
        }
        messages: list[dict[str, Any]] = []
        instructions = payload.get("instructions")
        if isinstance(instructions, str) and instructions:
            messages.append({"role": "system", "content": instructions})
        messages.extend(_input_to_messages(payload.get("input")))
        upstream["messages"] = messages
        limit = payload.get("max_output_tokens")
        if isinstance(limit, int) and not isinstance(limit, bool):
            upstream["max_tokens"] = limit
        tools = _tools_to_chat(payload.get("tools"))
        if tools:
            upstream["tools"] = tools
            choice = _tool_choice_to_chat(payload.get("tool_choice"))
            if choice is not None:
                upstream["tool_choice"] = choice
        reasoning = payload.get("reasoning")
        effort = reasoning.get("effort") if isinstance(reasoning, dict) else None
        if isinstance(effort, str) and effort:
            upstream["reasoning_effort"] = effort
        text = payload.get("text")
        fmt = text.get("format") if isinstance(text, dict) else None
        if isinstance(fmt, dict) and fmt.get("type") in {"json_object", "json_schema"}:
            upstream["response_format"] = fmt
        return upstream

    def translate_response(
        self, payload: dict[str, Any], context: TranslationContext
    ) -> dict[str, Any]:
        choices = payload.get("choices")
        choice = next(
            (item for item in choices if isinstance(item, dict)),
            {},
        ) if isinstance(choices, list) else {}
        message = choice.get("message")
        message = message if isinstance(message, dict) else {}
        identifier = response_id(payload.get("id"))
        output: list[dict[str, Any]] = []
        reasoning = message.get("reasoning_content") or message.get("reasoning")
        if isinstance(reasoning, str) and reasoning:
            output.append(
                {
                    "type": "reasoning",
                    "id": f"rs_{identifier}",
                    "summary": [{"type": "summary_text", "text": reasoning}],
                }
            )
        content = message.get("content")
        text = content if isinstance(content, str) else joined_text(content)
        if text:
            output.append(
                {
                    "type": "message",
                    "id": f"msg_{identifier}",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text, "annotations": []}],
                }
            )
        output.extend(_function_calls(message.get("tool_calls")))
        finish_reason = str(choice.get("finish_reason") or "stop")
        return {
            "id": identifier,
            "object": "response",
            "created_at": int(time.time()),
            "model": context.requested_model,
            "status": STATUS_FROM_FINISH_REASON.get(finish_reason, "completed"),
            "output": output,
            "usage": usage_to_responses(payload.get("usage")),
        }

    def stream(self, context: TranslationContext) -> StreamTranslator:
        return ChatToResponsesStream(context)


def _input_to_messages(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, str):
        return [{"role": "user", "content": value}] if value else []
    if not isinstance(value, list):
        return []
    messages: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "function_call":
            messages.append(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": str(item.get("call_id") or item.get("id") or ""),
                            "type": "function",
                            "function": {
                                "name": str(item.get("name") or ""),
                                "arguments": str(item.get("arguments") or "{}"),
                            },
                        }
                    ],
                }
            )
        elif kind == "function_call_output":
            output = item.get("output")
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": str(item.get("call_id") or ""),
                    "content": output
                    if isinstance(output, str)
                    else json.dumps(output, separators=(",", ":")),
                }
            )
        elif kind == "reasoning":
            # Reasoning items are Responses-only bookkeeping with no Chat Completions
            # slot. Replaying a summary as user or assistant text would change what
            # the model believes was said.
            continue
        else:
            role = item.get("role")
            if not isinstance(role, str) or not role:
                continue
            content = _input_content(item.get("content"))
            if content is not None:
                messages.append({"role": role, "content": content})
    return messages


def _input_content(content: Any) -> Any:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind in {"input_text", "output_text", "text"}:
            text = part.get("text")
            if isinstance(text, str) and text:
                parts.append({"type": "text", "text": text})
        elif kind in {"input_image", "image_url"}:
            url = part.get("image_url")
            url = url.get("url") if isinstance(url, dict) else url
            if isinstance(url, str) and url:
                parts.append({"type": "image_url", "image_url": {"url": url}})
    if not parts:
        return None
    if len(parts) == 1 and parts[0]["type"] == "text":
        return parts[0]["text"]
    return parts


def _tools_to_chat(tools: Any) -> list[dict[str, Any]] | None:
    if not isinstance(tools, list):
        return None
    converted: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            # Hosted Responses tools (web_search, file_search, computer_use) have no
            # Chat Completions equivalent; the request succeeds without them.
            continue
        name = tool.get("name")
        source = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        name = name if isinstance(name, str) and name else source.get("name")
        if not isinstance(name, str) or not name:
            continue
        function: dict[str, Any] = {"name": name}
        description = source.get("description")
        if isinstance(description, str) and description:
            function["description"] = description
        parameters = source.get("parameters")
        function["parameters"] = (
            parameters if isinstance(parameters, dict) else {"type": "object", "properties": {}}
        )
        converted.append({"type": "function", "function": function})
    return converted or None


def _tool_choice_to_chat(choice: Any) -> Any:
    if isinstance(choice, str):
        return choice if choice in {"auto", "none", "required"} else None
    if isinstance(choice, dict) and choice.get("type") == "function":
        name = choice.get("name")
        if isinstance(name, str) and name:
            return {"type": "function", "function": {"name": name}}
    return None


def _function_calls(tool_calls: Any) -> list[dict[str, Any]]:
    if not isinstance(tool_calls, list):
        return []
    items: list[dict[str, Any]] = []
    for position, call in enumerate(tool_calls):
        if not isinstance(call, dict):
            continue
        function = call.get("function")
        function = function if isinstance(function, dict) else {}
        call_id = str(call.get("id") or f"call_{position}")
        items.append(
            {
                "type": "function_call",
                "id": f"fc_{call_id}",
                "call_id": call_id,
                "status": "completed",
                "name": str(function.get("name") or ""),
                "arguments": str(function.get("arguments") or "{}"),
            }
        )
    return items

