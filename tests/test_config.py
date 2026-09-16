import pytest
from pydantic import ValidationError

from gateway.config import Settings


def test_settings_have_safe_local_defaults() -> None:
    settings = Settings(_env_file=None)

    assert settings.host == "127.0.0.1"
    assert settings.port == 8320
    assert settings.environment == "development"
    assert settings.trust_incoming_request_id is False
    assert settings.max_in_flight_requests == 32
    assert settings.max_request_body_bytes == 8 * 1024 * 1024
    assert settings.max_in_flight_body_bytes == 64 * 1024 * 1024


def test_settings_reject_invalid_port() -> None:
    with pytest.raises(ValidationError):
        Settings(port=0, _env_file=None)


def test_request_id_header_is_normalized() -> None:
    settings = Settings(request_id_header="X-Gateway-Request-ID", _env_file=None)

    assert settings.request_id_header == "x-gateway-request-id"


@pytest.mark.parametrize("name,value", [
    ("max_in_flight_requests", 0),
    ("max_request_body_bytes", 0),
    ("max_in_flight_body_bytes", 0),
    ("request_body_timeout_seconds", 0),
    ("request_body_timeout_seconds", float("inf")),
    ("overload_retry_after_seconds", 0),
])
def test_admission_settings_reject_unbounded_or_invalid_limits(name, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{name: value})
