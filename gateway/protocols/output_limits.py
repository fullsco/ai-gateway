"""One protocol-correct output ceiling for validation, translation and accounting."""

FIELDS = {"max_tokens", "max_completion_tokens", "max_output_tokens"}


def output_limit(protocol, payload, default):
    name = str(protocol)
    allowed = {"max_tokens", "max_completion_tokens"} if name == "openai_chat_completions" else {
        "max_output_tokens" if name == "openai_responses" else "max_tokens"
    }
    present = FIELDS.intersection(payload)
    if present - allowed or len(present) > 1:
        raise ValueError("Use one output-token limit supported by the requested protocol")
    value = payload[next(iter(present))] if present else default
    if type(value) is not int or value < 1:
        raise ValueError("Output-token limit must be a positive integer")
    return value


def with_output_limit(protocol, payload, default):
    limit = output_limit(protocol, payload, default)
    copied = dict(payload)
    if not FIELDS.intersection(copied):
        copied["max_output_tokens" if str(protocol) == "openai_responses" else "max_tokens"] = limit
    return copied, limit
