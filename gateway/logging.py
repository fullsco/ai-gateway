import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from gateway.context import get_request_id

REDACTED = "[REDACTED]"
SENSITIVE_FIELDS = {
    "authorization",
    "cookie",
    "set-cookie",
    "api_key",
    "apikey",
    "password",
    "secret",
    "token",
    "x-api-key",
}


def _is_sensitive(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(part in SENSITIVE_FIELDS for part in {key.lower(), normalized}) or any(
        marker in normalized for marker in ("password", "secret", "token", "api_key")
    )


def redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): REDACTED if _is_sensitive(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    return value


class GatewayFormatter(logging.Formatter):
    def __init__(self, output_format: str) -> None:
        super().__init__()
        self.output_format = output_format

    def format(self, record: logging.LogRecord) -> str:
        event = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if request_id := get_request_id():
            event["request_id"] = request_id
        if fields := getattr(record, "fields", None):
            event.update(redact(fields))
        if record.exc_info:
            event["exception"] = self.formatException(record.exc_info)
        if self.output_format == "json":
            return json.dumps(event, separators=(",", ":"), default=str)
        context = " ".join(f"{key}={value}" for key, value in event.items())
        return context


def configure_logging(level: str, output_format: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(GatewayFormatter(output_format))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


def log_event(logger: logging.Logger, level: int, message: str, **fields: Any) -> None:
    logger.log(level, message, extra={"fields": fields})
