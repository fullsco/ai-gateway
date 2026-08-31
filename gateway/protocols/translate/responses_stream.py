"""The Chat Completions -> OpenAI Responses stream re-framer.

Split from `responses_from_chat` so neither file carries both the body mapping and the
event-sequence state machine.
"""

import time
from typing import Any

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
        self.response_id = "resp_gateway_translated"
        self._created = int(time.time())
        self._sequence = 0
        self._started = False
        self._done = False
        self._text_item: int | None = None
        self._text = ""
        self._tool_items: dict[Any, dict[str, Any]] = {}
        self._next_output_index = 0
        self._finish_reason = "stop"
        self._usage: dict[str, Any] = {}

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
        identifier = payload.get("id")
        if isinstance(identifier, str) and identifier and not self._started:
            self.response_id = response_id(identifier)
        usage = payload.get("usage")
        if isinstance(usage, dict):
            self._usage = usage
        choices = payload.get("choices")
        choice = next((item for item in choices if isinstance(item, dict)), {}) if isinstance(
            choices, list
        ) else {}
        delta = choice.get("delta")
        delta = delta if isinstance(delta, dict) else {}
        out = bytearray(self._ensure_started())
        content = delta.get("content")
        text = content if isinstance(content, str) else joined_text(content)
        if text:
            out += self._text_delta(text)
        out += self._tool_deltas(delta.get("tool_calls"))
        finish_reason = choice.get("finish_reason")
        if isinstance(finish_reason, str) and finish_reason:
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
            item = self._tool_items.get(key)
            if item is None:
                call_id = str(call.get("id") or f"call_{key}")
                item = {
                    "output_index": self._next_output_index,
                    "id": f"fc_{call_id}",
                    "call_id": call_id,
                    "name": str(function.get("name") or ""),
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
        output: list[dict[str, Any]] = []
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
            output.append(message)
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
            output.append(call)
        status = STATUS_FROM_FINISH_REASON.get(self._finish_reason, "completed")
        name = "response.completed" if status == "completed" else "response.incomplete"
        out += self._emit(name, {"response": self._response(status=status, output=output)})
        return bytes(out)
