import logging
import random
import time
from collections.abc import MutableSequence
from typing import Any

from gateway.config import Settings
from gateway.context import reset_request_id, set_request_id
from gateway.logging import log_event
from gateway.metrics import current_trace, metrics, request_started
from gateway.request_ids import generate_request_id, is_valid_request_id

logger = logging.getLogger("gateway.http")


class RequestContextMiddleware:
    def __init__(self, app: Any, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {key.decode().lower(): value.decode() for key, value in scope["headers"]}
        incoming_id = headers.get(self.settings.request_id_header, "")
        request_id = (
            incoming_id
            if self.settings.trust_incoming_request_id and is_valid_request_id(incoming_id)
            else generate_request_id()
        )
        token = set_request_id(request_id)
        started = time.perf_counter()
        timing_token = request_started.set(started)
        status_code = 500
        trace = [] if random.random() < self.settings.trace_sample_rate else None
        trace_token = current_trace.set(trace)

        async def send_with_context(message: dict) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                response_headers: MutableSequence[tuple[bytes, bytes]] = message.setdefault(
                    "headers", []
                )
                response_headers.append(
                    (self.settings.request_id_header.encode(), request_id.encode())
                )
            await send(message)

        try:
            await self.app(scope, receive, send_with_context)
        finally:
            metrics.observe("request_total", time.perf_counter() - started, status_code >= 500)
            if trace is not None:
                metrics.traces.append({"status": status_code, "spans": trace})
            current_trace.reset(trace_token)
            request_started.reset(timing_token)
            log_event(
                logger,
                logging.INFO,
                "request_completed",
                method=scope["method"],
                path=scope["path"],
                status_code=status_code,
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
            )
            reset_request_id(token)
