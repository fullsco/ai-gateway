"""Local admission before inference parsing, authentication, or provider work."""

import asyncio
import json
from collections.abc import Callable
from typing import Any, Protocol

from starlette._utils import get_route_path
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from gateway.context import get_request_id
from gateway.distributed_capacity import capacity_lifetime
from gateway.metrics import measured

_INFERENCE_PATHS = frozenset({"/v1/messages", "/v1/chat/completions", "/v1/responses"})


class AdmissionSettings(Protocol):
    max_in_flight_requests: int
    max_request_body_bytes: int
    max_in_flight_body_bytes: int
    request_body_timeout_seconds: float
    overload_retry_after_seconds: int
    max_in_flight_per_client: int
    max_in_flight_streams: int


class _AdmissionError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        self.status = status
        self.code = code
        self.message = message


class InferenceAdmissionMiddleware:
    """One counter per middleware instance/process, shared by all inference APIs.

    Check-and-increment has no await, so admission is atomic on the ASGI event
    loop. Install once per app; this is not a cross-worker or cross-thread limit.
    Bodies are prebuffered (O(body limit) per admitted upload, with a transient
    copy on conversion to bytes), then the middleware's reference is released
    on replay. Downstream JSON parsing may require additional memory.
    """

    def __init__(self, app: ASGIApp, settings: AdmissionSettings, drain=None) -> None:
        self.app = app
        self.settings = settings
        self.drain = drain
        self._in_flight = 0
        self._body_bytes = 0
        self._clients: dict[str, int] = {}
        self._streams = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        path = get_route_path(scope).rstrip("/")
        workflow_write = path == "/v1/workflows" or path.startswith("/v1/workflows/")
        if path not in _INFERENCE_PATHS and not workflow_write:
            await self.app(scope, receive, send)
            return

        if self._in_flight >= self.settings.max_in_flight_requests:
            await self._reject(
                path, send, _AdmissionError(429, "overloaded", "The gateway is at capacity.")
            )
            return

        self._in_flight += 1
        if self.drain is not None and not self.drain.enter():
            self._in_flight -= 1
            await self._reject(path, send, _AdmissionError(503, "draining", "Gateway is draining."))
            return
        reserved_bytes = 0
        admitted_client: str | None = None
        admitted_stream = False
        distributed_pulse = None
        request_task = asyncio.current_task()

        def admit_client(client_id: str, stream: bool) -> bool:
            nonlocal admitted_client, admitted_stream
            if admitted_client is not None:
                return admitted_client == client_id and admitted_stream == stream
            if self._clients.get(client_id, 0) >= self.settings.max_in_flight_per_client:
                return False
            if stream and self._streams >= self.settings.max_in_flight_streams:
                return False
            self._clients[client_id] = self._clients.get(client_id, 0) + 1
            self._streams += int(stream)
            admitted_client, admitted_stream = client_id, stream
            return True

        scope.setdefault("state", {})["admit_client"] = admit_client

        def start_distributed(manager, owner, count):
            nonlocal distributed_pulse
            scope["state"]["distributed_admission"] = (manager, owner, count)
            distributed_pulse = manager.guard(owner, count, request_task)

        scope["state"]["start_distributed_admission"] = start_distributed

        def reserve_bytes(size: int) -> None:
            nonlocal reserved_bytes
            if self._body_bytes + size > self.settings.max_in_flight_body_bytes:
                raise _AdmissionError(429, "overloaded", "The gateway input capacity is full.")
            self._body_bytes += size
            reserved_bytes += size

        try:
            try:
                declared_length = self._content_length(scope)
                body = await self._read_body(receive, declared_length, reserve_bytes)
            except _AdmissionError as error:
                await self._reject(path, send, error)
                return
            if body is None:  # The client disconnected before a complete body.
                return

            async def replay_receive() -> Message:
                nonlocal body
                if body is not None:
                    message = {"type": "http.request", "body": body, "more_body": False}
                    body = None
                    return message
                # StreamingResponse must still be able to observe disconnects.
                return await receive()

            async with capacity_lifetime():
                await self.app(scope, replay_receive, send)
        finally:
            # These callbacks close over request state (including scope itself).
            # Detach them before any cleanup await can be interrupted.
            scope["state"].pop("admit_client", None)
            scope["state"].pop("start_distributed_admission", None)
            distributed = scope["state"].pop("distributed_admission", None)
            # Release synchronous local accounting even if asynchronous cleanup is cancelled.
            self._in_flight -= 1
            if self.drain is not None:
                self.drain.leave()
            self._body_bytes -= reserved_bytes
            if admitted_client is not None:
                remaining = self._clients[admitted_client] - 1
                if remaining:
                    self._clients[admitted_client] = remaining
                else:
                    del self._clients[admitted_client]
                self._streams -= int(admitted_stream)
            if distributed_pulse is not None:
                distributed_pulse.cancel()
                await asyncio.gather(distributed_pulse, return_exceptions=True)
            if distributed:
                manager, owner, _ = distributed
                try:
                    async with asyncio.timeout(2):
                        await manager.release(owner)
                except Exception:
                    pass  # TTL reclaims an unreachable coordination backend.

    def _content_length(self, scope: Scope) -> int | None:
        values = [
            value for key, value in scope.get("headers", []) if key.lower() == b"content-length"
        ]
        if not values:
            return None
        if len(values) != 1:
            raise _AdmissionError(400, "invalid_content_length", "Duplicate Content-Length header.")
        value = values[0].strip(b" \t")
        if not value or not value.isdigit():
            raise _AdmissionError(400, "invalid_content_length", "Invalid Content-Length header.")
        # Compare as decimal strings first, avoiding Python's huge-integer limit.
        value = value.lstrip(b"0") or b"0"
        maximum = str(self.settings.max_request_body_bytes).encode("ascii")
        if len(value) > len(maximum) or (len(value) == len(maximum) and value > maximum):
            raise _AdmissionError(413, "request_too_large", "Request body exceeds the size limit.")
        return int(value)

    @measured("request_upload")
    async def _read_body(
        self, receive: Receive, declared_length: int | None,
        reserve_bytes: Callable[[int], None],
    ) -> bytes | None:
        buffered = bytearray()
        try:
            async with asyncio.timeout(self.settings.request_body_timeout_seconds):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return None
                    chunk = message.get("body", b"")
                    size = len(buffered) + len(chunk)
                    if size > self.settings.max_request_body_bytes:
                        raise _AdmissionError(
                            413, "request_too_large", "Request body exceeds the size limit."
                        )
                    if declared_length is not None and size > declared_length:
                        raise _AdmissionError(
                            400,
                            "invalid_content_length",
                            "Request body does not match Content-Length.",
                        )
                    # Charge actual bytes before buffering. Keep the charge through
                    # response completion because downstream retains parsed input.
                    reserve_bytes(len(chunk))
                    buffered.extend(chunk)
                    if not message.get("more_body", False):
                        if declared_length is not None and size != declared_length:
                            raise _AdmissionError(
                                400,
                                "invalid_content_length",
                                "Request body does not match Content-Length.",
                            )
                        return bytes(buffered)
        except TimeoutError as error:
            raise _AdmissionError(
                408, "request_body_timeout", "Timed out receiving the request body."
            ) from error

    async def _reject(self, path: str, send: Send, error: _AdmissionError) -> None:
        error_type = "rate_limit_error" if error.status == 429 else "invalid_request_error"
        detail: dict[str, Any] = {
            "type": error_type,
            "message": error.message,
            "request_id": get_request_id(),
        }
        if path == "/v1/messages":
            payload = {"type": "error", "error": detail}
        else:
            detail.update(param=None, code=error.code)
            payload = {"error": detail}
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ]
        if error.status == 429:
            headers.append(
                (b"retry-after", str(self.settings.overload_retry_after_seconds).encode())
            )
        await send({"type": "http.response.start", "status": error.status, "headers": headers})
        await send({"type": "http.response.body", "body": body})
