"""Mapping tables shared by both directions of Anthropic Messages <-> Chat Completions.

Kept in one place because the two directions must agree: a `tool_use` block that
becomes a `tool_calls` entry has to come back as the same block, and a stop reason
has to survive a round trip. Two private copies of these tables drift.
"""

import json
from typing import Any

# Anthropic reports why generation ended with more precision than Chat Completions.
# Chat Completions cannot distinguish a natural end from a stop-sequence hit, so the
# mapping back is deliberately lossy in that one direction and `stop_sequence` is
# reported as null rather than guessed.
STOP_REASON_FROM_FINISH_REASON = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "refusal",
}

FINISH_REASON_FROM_STOP_REASON = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "pause_turn": "stop",
    "refusal": "content_filter",
}

TOOL_CHOICE_FROM_ANTHROPIC = {"auto": "auto", "any": "required", "none": "none"}

# Anthropic exposes a token budget for thinking; Chat Completions exposes a coarse
# effort dial. Bucketed rather than dropped so a request that asks to think still
# thinks, at roughly the depth it asked for.
_REASONING_EFFORT_BUDGETS = ((2048, "low"), (8192, "medium"))


def reasoning_effort_from_thinking(thinking: Any) -> str | None:
    if not isinstance(thinking, dict) or thinking.get("type") == "disabled":
        return None
    budget = thinking.get("budget_tokens")
    if not isinstance(budget, int) or isinstance(budget, bool) or budget <= 0:
        return "medium"
    for ceiling, effort in _REASONING_EFFORT_BUDGETS:
        if budget <= ceiling:
            return effort
    return "high"


def thinking_from_reasoning_effort(effort: Any) -> dict[str, Any] | None:
    budgets = {"low": 2048, "medium": 8192, "high": 16384}
    budget = budgets.get(str(effort).strip().lower()) if effort is not None else None
    if budget is None:
        return None
    return {"type": "enabled", "budget_tokens": budget}


def tools_to_openai(tools: Any) -> list[dict[str, Any]] | None:
    """Anthropic `tools` -> Chat Completions `tools`.

    Anthropic server tools (`type: web_search_20250305` and friends) have no Chat
    Completions equivalent and are dropped rather than sent as a function the
    upstream would reject: the request still succeeds without that one tool.
    """
    if not isinstance(tools, list):
        return None
    converted: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            continue
        declared = tool.get("type")
        if isinstance(declared, str) and declared not in {"custom", "function"}:
            continue
        function: dict[str, Any] = {"name": name}
        description = tool.get("description")
        if isinstance(description, str) and description:
            function["description"] = description
        schema = tool.get("input_schema")
        if not isinstance(schema, dict):
            schema = tool.get("parameters")
        function["parameters"] = (
            schema if isinstance(schema, dict) else {"type": "object", "properties": {}}
        )
        converted.append({"type": "function", "function": function})
    return converted or None


def tools_to_anthropic(tools: Any) -> list[dict[str, Any]] | None:
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
        entry: dict[str, Any] = {"name": name}
        description = source.get("description")
        if isinstance(description, str) and description:
            entry["description"] = description
        schema = source.get("parameters")
        entry["input_schema"] = (
            schema if isinstance(schema, dict) else {"type": "object", "properties": {}}
        )
        converted.append(entry)
    return converted or None


def tool_choice_to_openai(choice: Any) -> Any:
    if isinstance(choice, str):
        return TOOL_CHOICE_FROM_ANTHROPIC.get(choice, choice)
    if not isinstance(choice, dict):
        return None
    kind = choice.get("type")
    if kind == "tool":
        name = choice.get("name")
        if isinstance(name, str) and name:
            return {"type": "function", "function": {"name": name}}
        return "required"
    return TOOL_CHOICE_FROM_ANTHROPIC.get(str(kind))


def tool_choice_to_anthropic(choice: Any) -> Any:
    if isinstance(choice, str):
        mapping = {"auto": {"type": "auto"}, "required": {"type": "any"}, "none": {"type": "none"}}
        return mapping.get(choice)
    if isinstance(choice, dict):
        function = choice.get("function")
        name = function.get("name") if isinstance(function, dict) else None
        if isinstance(name, str) and name:
            return {"type": "tool", "name": name}
    return None


def usage_to_anthropic(usage: Any) -> dict[str, Any]:
    """Chat Completions `usage` -> Anthropic `usage`.

    The two conventions disagree on what "input" counts: OpenAI's prompt total
    includes cached tokens, Anthropic's `input_tokens` excludes them. Subtracting
    here keeps an Anthropic client's own arithmetic (`input + cache_read`) correct.
    Gateway-side accounting is unaffected: it reads the upstream bytes under the
    upstream protocol, never this translated view.
    """
    if not isinstance(usage, dict):
        return {"input_tokens": 0, "output_tokens": 0}
    prompt = _int(usage.get("prompt_tokens")) or _int(usage.get("input_tokens")) or 0
    completion = _int(usage.get("completion_tokens")) or _int(usage.get("output_tokens")) or 0
    details = usage.get("prompt_tokens_details")
    cached = _int(details.get("cached_tokens")) if isinstance(details, dict) else None
    translated: dict[str, Any] = {
        "input_tokens": max(0, prompt - (cached or 0)),
        "output_tokens": completion,
    }
    if cached:
        translated["cache_read_input_tokens"] = cached
    return translated


def usage_to_openai(usage: Any) -> dict[str, Any] | None:
    if not isinstance(usage, dict):
        return None
    reported = _int(usage.get("input_tokens")) or 0
    cache_read = _int(usage.get("cache_read_input_tokens")) or 0
    cache_write = _int(usage.get("cache_creation_input_tokens")) or 0
    output = _int(usage.get("output_tokens")) or 0
    prompt = reported + cache_read + cache_write
    translated: dict[str, Any] = {
        "prompt_tokens": prompt,
        "completion_tokens": output,
        "total_tokens": prompt + output,
    }
    if cache_read:
        translated["prompt_tokens_details"] = {"cached_tokens": cache_read}
    return translated


def content_parts_to_openai(blocks: Any) -> tuple[Any, list[dict[str, Any]]]:
    """Anthropic content blocks -> (Chat Completions content, tool_result messages).

    `tool_result` is a content block for Anthropic but a whole message for OpenAI, so
    it cannot stay inline; it is returned separately for the caller to place before
    the remaining content, which is the order OpenAI requires.
    """
    if isinstance(blocks, str):
        return blocks, []
    if not isinstance(blocks, list):
        return None, []
    parts: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                parts.append({"type": "text", "text": text})
        elif kind == "image":
            url = _image_url(block.get("source"))
            if url is not None:
                parts.append({"type": "image_url", "image_url": {"url": url}})
        elif kind == "tool_result":
            tool_results.append(_tool_result_message(block))
    if not parts:
        return None, tool_results
    if len(parts) == 1 and parts[0]["type"] == "text":
        # A single text part is sent as a bare string: some OpenAI-compatible
        # upstreams reject the parts array for text-only messages.
        return parts[0]["text"], tool_results
    return parts, tool_results


def _tool_result_message(block: dict[str, Any]) -> dict[str, Any]:
    content = block.get("content")
    if isinstance(content, list):
        text = "\n".join(
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        )
    elif isinstance(content, str):
        text = content
    else:
        text = "" if content is None else json.dumps(content, separators=(",", ":"))
    if block.get("is_error") is True and text:
        text = f"Error: {text}"
    return {
        "role": "tool",
        "tool_call_id": str(block.get("tool_use_id") or ""),
        "content": text,
    }


def content_parts_to_anthropic(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        return []
    blocks: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, str):
            if part:
                blocks.append({"type": "text", "text": part})
            continue
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind == "text":
            text = part.get("text")
            if isinstance(text, str) and text:
                blocks.append({"type": "text", "text": text})
        elif kind == "image_url":
            source = _image_source(part.get("image_url"))
            if source is not None:
                blocks.append({"type": "image", "source": source})
    return blocks


def _image_url(source: Any) -> str | None:
    if not isinstance(source, dict):
        return None
    kind = source.get("type")
    if kind == "url":
        url = source.get("url")
        return url if isinstance(url, str) and url else None
    if kind == "base64":
        data = source.get("data")
        media_type = source.get("media_type") or "image/png"
        if isinstance(data, str) and data:
            return f"data:{media_type};base64,{data}"
    return None


def _image_source(image_url: Any) -> dict[str, Any] | None:
    url = image_url.get("url") if isinstance(image_url, dict) else image_url
    if not isinstance(url, str) or not url:
        return None
    if not url.startswith("data:"):
        return {"type": "url", "url": url}
    header, _, data = url[len("data:") :].partition(",")
    media_type, _, encoding = header.partition(";")
    if encoding.strip().lower() != "base64" or not data:
        return None
    return {
        "type": "base64",
        "media_type": media_type.strip() or "image/png",
        "data": data,
    }


def tool_call_input(arguments: Any) -> dict[str, Any]:
    """A tool call's arguments as Anthropic's decoded object.

    Anthropic carries tool input as JSON; Chat Completions carries it as a string the
    model generated, which is not guaranteed to parse. An unparseable string is
    surfaced under a key rather than dropped, so a malformed call is visible to the
    client instead of arriving as an empty call.
    """
    if isinstance(arguments, dict):
        return arguments
    if not isinstance(arguments, str) or not arguments.strip():
        return {}
    try:
        parsed = json.loads(arguments)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"__unparsed_arguments": arguments}
    return parsed if isinstance(parsed, dict) else {"__unparsed_arguments": arguments}


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
