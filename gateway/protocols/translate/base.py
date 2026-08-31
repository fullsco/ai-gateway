"""Shared machinery for translating between two client protocols.

A translator is named for its direction: ``client <- upstream``. ``translate_request``
maps a client payload onto the upstream API, ``translate_response`` maps an upstream
body back, and ``stream`` returns a stateful object fed upstream SSE bytes that yields
client SSE bytes. Everything here is pure and HTTP-free so each pair is unit-testable
without a provider.
"""

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from gateway.protocols.models import ClientProtocol


@dataclass(frozen=True)
class TranslationContext:
    """What a translator cannot recover from the upstream bytes alone.

    The client asked for a canonical model name; the upstream answers with its own
    model id, and echoing that back leaks the routing decision into the client's
    transcript. Whether extended thinking was requested is likewise only knowable
    from the request, and it decides whether upstream reasoning text is surfaced.
    """

    requested_model: str
    reasoning_requested: bool = False


@dataclass(frozen=True)
class SSEEvent:
    name: str | None
    data: str

    def json(self) -> dict[str, Any] | None:
        """The event's payload, or None when it is not a JSON object."""
        if self.data == "[DONE]":
            return None
        try:
            parsed = json.loads(self.data)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        return parsed if isinstance(parsed, dict) else None

    @property
    def is_done(self) -> bool:
        return self.data.strip() == "[DONE]"


class SSEDecoder:
    """Reassembles SSE events from raw upstream network chunks.

    Chunks are under no obligation to align to event boundaries -- the same fact that
    forces ``_ends_on_event_boundary`` in the executor -- so a partial event is held
    until the rest of it arrives. Emitting on a half-read frame would hand the client
    truncated JSON, which is worse than a slightly later frame.
    """

    MAX_EVENT_BYTES = 1024 * 1024

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> list[SSEEvent]:
        self._buffer.extend(chunk)
        events: list[SSEEvent] = []
        while (separator := self._separator()) is not None:
            index, length = separator
            raw = bytes(self._buffer[:index])
            del self._buffer[: index + length]
            event = self._parse(raw)
            if event is not None:
                events.append(event)
        if len(self._buffer) > self.MAX_EVENT_BYTES:
            # A stream with no event boundary this large is not SSE. Dropping the
            # buffer bounds memory rather than growing it for the life of the request.
            self._buffer.clear()
        return events

    def flush(self) -> list[SSEEvent]:
        """Any trailing event an upstream ended without a blank line after it."""
        if not self._buffer.strip():
            self._buffer.clear()
            return []
        raw = bytes(self._buffer)
        self._buffer.clear()
        event = self._parse(raw)
        return [event] if event is not None else []

    def _separator(self) -> tuple[int, int] | None:
        crlf = self._buffer.find(b"\r\n\r\n")
        lf = self._buffer.find(b"\n\n")
        present = [candidate for candidate in ((crlf, 4), (lf, 2)) if candidate[0] >= 0]
        return min(present) if present else None

    @staticmethod
    def _parse(raw: bytes) -> SSEEvent | None:
        name: str | None = None
        data_lines: list[str] = []
        for line in raw.splitlines():
            if line.startswith(b":"):
                # SSE comment. Providers use these as keepalive padding; they carry
                # nothing a client needs and must not be mistaken for a data frame.
                continue
            field, _, value = line.partition(b":")
            key = field.decode("utf-8", "replace").strip()
            text = value.decode("utf-8", "replace")
            if text.startswith(" "):
                text = text[1:]
            if key == "event":
                name = text.strip()
            elif key == "data":
                data_lines.append(text)
        if not data_lines:
            return None
        return SSEEvent(name=name, data="\n".join(data_lines))


def format_sse(name: str | None, data: Any) -> bytes:
    body = data if isinstance(data, str) else json.dumps(data, separators=(",", ":"))
    prefix = f"event: {name}\n" if name else ""
    return f"{prefix}data: {body}\n\n".encode()


class StreamTranslator(ABC):
    """Converts one protocol's SSE stream into another's, incrementally."""

    @abstractmethod
    def feed(self, chunk: bytes) -> bytes:
        """Client-protocol bytes owed for the upstream bytes consumed so far."""

    @abstractmethod
    def finish(self) -> bytes:
        """Frames still owed once the upstream stream ends cleanly."""


class ProtocolTranslator(ABC):
    """A single ``client_protocol <- upstream_protocol`` direction."""

    client_protocol: ClientProtocol
    upstream_protocol: ClientProtocol

    @abstractmethod
    def translate_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        """A client request payload expressed in the upstream protocol."""

    @abstractmethod
    def translate_response(
        self, payload: dict[str, Any], context: TranslationContext
    ) -> dict[str, Any]:
        """An upstream non-streaming body expressed in the client protocol."""

    @abstractmethod
    def stream(self, context: TranslationContext) -> StreamTranslator:
        """A fresh stream translator for one request."""


class ChainedTranslator(ProtocolTranslator):
    """``A <- C`` composed from ``A <- B`` and ``B <- C``.

    The matrix has six ordered pairs but only two shapes worth mapping by hand;
    routing the rest through an intermediate protocol keeps one mapping table per
    pair of *shapes* instead of one per pair of protocols, so a fix to the Anthropic
    content-block rules cannot drift between two copies.
    """

    def __init__(self, outer: ProtocolTranslator, inner: ProtocolTranslator) -> None:
        if outer.upstream_protocol is not inner.client_protocol:
            raise ValueError(
                "Chained translators must meet on a shared protocol: "
                f"{outer.upstream_protocol.value} != {inner.client_protocol.value}"
            )
        self._outer = outer
        self._inner = inner
        self.client_protocol = outer.client_protocol
        self.upstream_protocol = inner.upstream_protocol

    def translate_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._inner.translate_request(self._outer.translate_request(payload))

    def translate_response(
        self, payload: dict[str, Any], context: TranslationContext
    ) -> dict[str, Any]:
        intermediate = self._inner.translate_response(payload, context)
        return self._outer.translate_response(intermediate, context)

    def stream(self, context: TranslationContext) -> StreamTranslator:
        return _ChainedStream(self._outer.stream(context), self._inner.stream(context))


class _ChainedStream(StreamTranslator):
    def __init__(self, outer: StreamTranslator, inner: StreamTranslator) -> None:
        self._outer = outer
        self._inner = inner

    def feed(self, chunk: bytes) -> bytes:
        return self._outer.feed(self._inner.feed(chunk))

    def finish(self) -> bytes:
        # The inner stream's closing frames are input to the outer one, which only
        # then emits its own terminator.
        return self._outer.feed(self._inner.finish()) + self._outer.finish()
