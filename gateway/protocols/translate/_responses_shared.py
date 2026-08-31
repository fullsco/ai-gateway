"""Helpers shared by the OpenAI Responses body mapper and its stream re-framer."""

from typing import Any

# Responses reports a terminal status where Chat Completions reports why the model
# stopped. `length` is the only finish reason that is not a clean completion, so it is
# the only one that becomes `incomplete`.
STATUS_FROM_FINISH_REASON = {
    "stop": "completed",
    "tool_calls": "completed",
    "function_call": "completed",
    "length": "incomplete",
    "content_filter": "incomplete",
}


def joined_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        part["text"]
        for part in content
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    )


def usage_to_responses(usage: Any) -> dict[str, Any]:
    """Chat Completions `usage` -> Responses `usage`.

    Both protocols count the prompt inclusively, so unlike the Anthropic mapping this
    is a rename rather than a change of convention.
    """
    if not isinstance(usage, dict):
        return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    prompt = _int(usage.get("prompt_tokens")) or _int(usage.get("input_tokens")) or 0
    completion = _int(usage.get("completion_tokens")) or _int(usage.get("output_tokens")) or 0
    details = usage.get("prompt_tokens_details")
    cached = _int(details.get("cached_tokens")) if isinstance(details, dict) else None
    translated: dict[str, Any] = {
        "input_tokens": prompt,
        "output_tokens": completion,
        "total_tokens": prompt + completion,
    }
    if cached:
        translated["input_tokens_details"] = {"cached_tokens": cached}
    return translated


def response_id(upstream_id: Any) -> str:
    if isinstance(upstream_id, str) and upstream_id:
        return upstream_id if upstream_id.startswith("resp_") else f"resp_{upstream_id}"
    return "resp_gateway_translated"


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
