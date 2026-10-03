"""Protocol terminal evidence for financial settlement; EOF alone is not success."""

import json

from gateway.protocols.event_buffer import EventBuffer


class StreamCompletion:
    def __init__(self, protocol):
        self.protocol = str(protocol)
        self.frames = EventBuffer(1024 * 1024, discard_oversize=True)
        self.complete = False
        self.failed = False

    def feed(self, chunk):
        for raw in self.frames.feed(chunk):
            data = b"\n".join(line.partition(b":")[2].lstrip() for line in raw.splitlines()
                               if line.startswith(b"data:"))
            if data.strip() == b"[DONE]" and self.protocol == "openai_chat_completions":
                self.complete = True
                continue
            try:
                event = json.loads(data)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(event, dict):
                continue
            event_name = next((line.partition(b":")[2].strip().decode("utf-8", "replace")
                               for line in raw.splitlines() if line.startswith(b"event:")), None)
            kind = event.get("type", event_name)
            if kind in {"error", "response.failed", "response.incomplete"} or event.get("error"):
                self.failed = True
            if self.protocol == "anthropic_messages" and kind == "message_stop":
                self.complete = True
            if self.protocol == "openai_responses" and kind == "response.completed":
                self.complete = True

    @property
    def successful(self):
        return self.complete and not self.failed
