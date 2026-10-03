"""The Chat Completions -> OpenAI Responses stream re-framer.

Split from `responses_from_chat` so neither file carries both the body mapping and the
event-sequence state machine.
"""

import json
import time
from typing import Any

from gateway.protocols.event_buffer import EventTooLarge
from gateway.protocols.translate._responses_shared import (
    STATUS_FROM_FINISH_REASON,
    joined_text,
    response_id,
    usage_to_responses,
)
from gateway.protocols.translate.base import (
    SSEDecoder,
    SSEEvent,
    StreamTranslator,
    TranslationContext,
    format_sse,
)


class _StreamRetention:
    """Shared retained-state budget for Chat -> Responses/Anthropic streams.

    Payload bytes are UTF-8, including serialized metadata. Separate call and
    fragment caps bound container overhead even for empty calls/tiny deltas.
    Failure is sticky: a caller cannot resume or synthesize a successful close.
    """

    MAX_BYTES = SSEDecoder.MAX_EVENT_BYTES
    MAX_TOOL_CALLS = 1024
    MAX_ARGUMENT_FRAGMENTS = 16384

    def __init__(self) -> None:
        self.bytes = self.calls = self.fragments = 0
        self._metadata: dict[str, int] = {}
        self._error: str | None = None

    def check(self) -> None:
        if self._error is not None:
            raise EventTooLarge(self._error)

    def reserve(self, *values: str, calls: int = 0, fragments: int = 0) -> None:
        self._reserve(sum(len(v.encode("utf-8", "surrogatepass")) for v in values),
                      calls, fragments)

    def metadata(self, key: str, value: Any) -> None:
        size = len(json.dumps(value, ensure_ascii=False).encode("utf-8", "surrogatepass"))
        self._reserve(size - self._metadata.get(key, 0), 0, 0)
        self._metadata[key] = size

    def _reserve(self, size: int, calls: int, fragments: int) -> None:
        self.check()
        if (self.bytes + size > self.MAX_BYTES or self.calls + calls > self.MAX_TOOL_CALLS
                or self.fragments + fragments > self.MAX_ARGUMENT_FRAGMENTS):
            self._error = "Upstream stream retained state exceeds the supported limit"
            raise EventTooLarge(self._error)
        self.bytes += size
        self.calls += calls
        self.fragments += fragments

    def tool_key(self, call: dict[str, Any], position: int) -> int:
        key = call.get("index", position)
        if type(key) is not int or key < 0:
            self._error = "Upstream stream tool call index must be a non-negative integer"
            self.check()
        return key


class ChatToResponsesStream(StreamTranslator):
    """Frames Chat Completions deltas as the Responses event sequence.

    Responses names every event and numbers output items, so the same structure the
    Anthropic translator has to invent is invented here too, against a different
    vocabulary: one `message` item holding a text part, one `function_call` item per
    tool call, and a terminal `response.completed` carrying usage.
    """

    def __init__(self, context: TranslationContext) -> None:
        self._context = context
        self._decoder = SSEDecoder()
        self._retained = _StreamRetention()
        self.response_id = "resp_gateway_translated"
        self._retained.metadata("id", self.response_id)
        self._created = int(time.time())
        self._sequence = 0
        self._started = False
        self._done = False
        self._text_item: int | None = None
        self._text = ""
        self._reasoning_item: int | None = None
        self._reasoning = ""
        self._tool_items: dict[Any, dict[str, Any]] = {}
        self._next_output_index = 0
        self._finish_reason = "stop"
        self._usage: dict[str, Any] = {}

    def feed(self, chunk: bytes) -> bytes:
        self._retained.check()
        if self._done:
            return b""
        out = bytearray()
        for event in self._decoder.feed(chunk):
            out += self._handle(event)
        return bytes(out)

    def finish(self) -> bytes:
        self._retained.check()
        out = bytearray()
        for event in self._decoder.flush():
            out += self._handle(event)
        out += self._close()
        return bytes(out)

    def _handle(self, event: SSEEvent) -> bytes:
        if self._done:
            return b""
        if event.is_done:
            return self._close()
        payload = event.json()
        if payload is None:
            return b""
        identifier = payload.get("id")
        if isinstance(identifier, str) and identifier and not self._started:
            self._retained.metadata("id", response_id(identifier))
            self.response_id = response_id(identifier)
        usage = payload.get("usage")
        if isinstance(usage, dict):
            self._retained.metadata("usage", usage)
            self._usage = usage
        choices = payload.get("choices")
        choice = next((item for item in choices if isinstance(item, dict)), {}) if isinstance(
            choices, list
        ) else {}
        delta = choice.get("delta")
        delta = delta if isinstance(delta, dict) else {}
        out = bytearray(self._ensure_started())
        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
        if self._context.reasoning_requested and isinstance(reasoning, str) and reasoning:
            out += self._reasoning_delta(reasoning)
        content = delta.get("content")
        text = content if isinstance(content, str) else joined_text(content)
        if text:
            out += self._text_delta(text)
        out += self._tool_deltas(delta.get("tool_calls"))
        finish_reason = choice.get("finish_reason")
        if isinstance(finish_reason, str) and finish_reason:
            self._retained.metadata("finish_reason", finish_reason)
            self._finish_reason = finish_reason
        return bytes(out)

    def _emit(self, name: str, body: dict[str, Any]) -> bytes:
        body = {"type": name, "sequence_number": self._sequence, **body}
        self._sequence += 1
        return format_sse(name, body)

    def _ensure_started(self) -> bytes:
        if self._started:
            return b""
        self._started = True
        skeleton = self._response(status="in_progress", output=[])
        return self._emit("response.created", {"response": skeleton}) + self._emit(
            "response.in_progress", {"response": skeleton}
        )

    def _text_delta(self, text: str) -> bytes:
        self._retained.reserve(text)
        out = bytearray()
        if self._text_item is None:
            self._text_item = self._next_output_index
            self._next_output_index += 1
            out += self._emit(
                "response.output_item.added",
                {
                    "output_index": self._text_item,
                    "item": {
                        "type": "message",
                        "id": f"msg_{self.response_id}",
                        "status": "in_progress",
                        "role": "assistant",
                        "content": [],
                    },
                },
            )
            out += self._emit(
                "response.content_part.added",
                {
                    "item_id": f"msg_{self.response_id}",
                    "output_index": self._text_item,
                    "content_index": 0,
                    "part": {"type": "output_text", "text": "", "annotations": []},
                },
            )
        self._text += text
        out += self._emit(
            "response.output_text.delta",
            {
                "item_id": f"msg_{self.response_id}",
                "output_index": self._text_item,
                "content_index": 0,
                "delta": text,
            },
        )
        return bytes(out)

    def _reasoning_delta(self, text: str) -> bytes:
        self._retained.reserve(text)
        out = bytearray()
        if self._reasoning_item is None:
            self._reasoning_item = self._next_output_index
            self._next_output_index += 1
            out += self._emit("response.output_item.added", {
                "output_index": self._reasoning_item,
                "item": {"type": "reasoning", "id": f"rs_{self.response_id}", "summary": []},
            })
            out += self._emit("response.reasoning_summary_part.added", {
                **self._reasoning_position(), "part": {"type": "summary_text", "text": ""},
            })
        self._reasoning += text
        out += self._emit("response.reasoning_summary_text.delta", {
            **self._reasoning_position(), "delta": text,
        })
        return bytes(out)

    def _reasoning_position(self) -> dict[str, Any]:
        return {"item_id": f"rs_{self.response_id}",
                "output_index": self._reasoning_item, "summary_index": 0}

    def _tool_deltas(self, calls: Any) -> bytes:
        if not isinstance(calls, list):
            return b""
        out = bytearray()
        for position, call in enumerate(calls):
            if not isinstance(call, dict):
                continue
            key = self._retained.tool_key(call, position)
            function = call.get("function")
            function = function if isinstance(function, dict) else {}
            item = self._tool_items.get(key)
            if item is None:
                call_id = str(call.get("id") or f"call_{key}")
                name = str(function.get("name") or "")
                self._retained.reserve(str(key), call_id, f"fc_{call_id}", name, calls=1)
                item = {
                    "output_index": self._next_output_index,
                    "id": f"fc_{call_id}",
                    "call_id": call_id,
                    "name": name,
                    "arguments": "",
                }
                self._next_output_index += 1
                self._tool_items[key] = item
                out += self._emit(
                    "response.output_item.added",
                    {
                        "output_index": item["output_index"],
                        "item": {
                            "type": "function_call",
                            "id": item["id"],
                            "call_id": call_id,
                            "status": "in_progress",
                            "name": item["name"],
                            "arguments": "",
                        },
                    },
                )
            arguments = function.get("arguments")
            if isinstance(arguments, str) and arguments:
                self._retained.reserve(arguments)
                item["arguments"] += arguments
                out += self._emit(
                    "response.function_call_arguments.delta",
                    {
                        "item_id": item["id"],
                        "output_index": item["output_index"],
                        "delta": arguments,
                    },
                )
        return bytes(out)

    def _response(self, *, status: str, output: list[dict[str, Any]]) -> dict[str, Any]:
        response: dict[str, Any] = {
            "id": self.response_id,
            "object": "response",
            "created_at": self._created,
            "model": self._context.requested_model,
            "status": status,
            "output": output,
        }
        if status != "in_progress":
            response["usage"] = usage_to_responses(self._usage)
        return response

    def _close(self) -> bytes:
        if self._done:
            return b""
        self._done = True
        out = bytearray(self._ensure_started())
        output: dict[int, dict[str, Any]] = {}
        if self._reasoning_item is not None:
            part = {"type": "summary_text", "text": self._reasoning}
            reasoning = {"type": "reasoning", "id": f"rs_{self.response_id}", "summary": [part]}
            out += self._emit("response.reasoning_summary_text.done", {
                **self._reasoning_position(), "text": self._reasoning,
            })
            out += self._emit("response.reasoning_summary_part.done", {
                **self._reasoning_position(), "part": part,
            })
            out += self._emit("response.output_item.done", {
                "output_index": self._reasoning_item, "item": reasoning,
            })
            output[self._reasoning_item] = reasoning
        if self._text_item is not None:
            item_id = f"msg_{self.response_id}"
            out += self._emit(
                "response.output_text.done",
                {
                    "item_id": item_id,
                    "output_index": self._text_item,
                    "content_index": 0,
                    "text": self._text,
                },
            )
            part = {"type": "output_text", "text": self._text, "annotations": []}
            out += self._emit(
                "response.content_part.done",
                {
                    "item_id": item_id,
                    "output_index": self._text_item,
                    "content_index": 0,
                    "part": part,
                },
            )
            message = {
                "type": "message",
                "id": item_id,
                "status": "completed",
                "role": "assistant",
                "content": [part],
            }
            out += self._emit(
                "response.output_item.done",
                {"output_index": self._text_item, "item": message},
            )
            output[self._text_item] = message
        for item in self._tool_items.values():
            out += self._emit(
                "response.function_call_arguments.done",
                {
                    "item_id": item["id"],
                    "output_index": item["output_index"],
                    "arguments": item["arguments"],
                },
            )
            call = {
                "type": "function_call",
                "id": item["id"],
                "call_id": item["call_id"],
                "status": "completed",
                "name": item["name"],
                "arguments": item["arguments"],
            }
            out += self._emit(
                "response.output_item.done", {"output_index": item["output_index"], "item": call}
            )
            output[item["output_index"]] = call
        status = STATUS_FROM_FINISH_REASON.get(self._finish_reason, "completed")
        name = "response.completed" if status == "completed" else "response.incomplete"
        out += self._emit(name, {"response": self._response(
            status=status, output=[output[index] for index in sorted(output)],
        )})
        return bytes(out)
