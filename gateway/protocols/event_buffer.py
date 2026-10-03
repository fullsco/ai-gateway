"""Incremental SSE framing with a strict per-event memory limit."""


class EventTooLarge(ValueError):
    pass


class EventBuffer:
    def __init__(self, limit: int, *, discard_oversize: bool = False):
        self.limit = limit
        self.discard_oversize = discard_oversize
        self.buffer = bytearray()
        self.discarding = False

    def feed(self, chunk: bytes):
        # Slice input before copying so one huge transport chunk cannot defeat the cap.
        for offset in range(0, len(chunk), 4096):
            self.buffer.extend(chunk[offset:offset + 4096])
            while True:
                choices = [(self.buffer.find(delimiter), len(delimiter))
                           for delimiter in (b"\n\n", b"\r\n\r\n")]
                candidates = [choice for choice in choices if choice[0] >= 0]
                if not candidates:
                    break
                index, size = min(candidates)
                if not self.discarding and index <= self.limit:
                    yield bytes(self.buffer[:index])
                elif not self.discard_oversize:
                    self.buffer.clear()
                    raise EventTooLarge("Upstream SSE event exceeds the supported limit")
                del self.buffer[:index + size]
                self.discarding = False
            if len(self.buffer) > self.limit:
                if not self.discard_oversize:
                    self.buffer.clear()
                    raise EventTooLarge("Upstream SSE event exceeds the supported limit")
                self.discarding = True
                del self.buffer[:-3]  # Retain only a possible partial delimiter.
            elif self.discarding and len(self.buffer) > 3:
                del self.buffer[:-3]

    def flush(self):
        value = bytes(self.buffer) if not self.discarding else b""
        self.buffer.clear()
        self.discarding = False
        return value
