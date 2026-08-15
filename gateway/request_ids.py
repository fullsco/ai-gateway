import re
import secrets
import time

REQUEST_ID_PATTERN = re.compile(r"^gw_[0-9a-f]{12}_[0-9a-f]{16}$")


def generate_request_id() -> str:
    timestamp_ms = int(time.time() * 1000)
    return f"gw_{timestamp_ms:012x}_{secrets.token_hex(8)}"


def is_valid_request_id(value: str) -> bool:
    return bool(REQUEST_ID_PATTERN.fullmatch(value))
