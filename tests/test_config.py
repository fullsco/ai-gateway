import pytest
from pydantic import ValidationError

from gateway.config import Settings


def test_settings_have_safe_local_defaults() -> None:
    settings = Settings(_env_file=None)

    assert settings.host == "127.0.0.1"
    assert settings.port == 8320
    assert settings.environment == "development"
    assert settings.trust_incoming_request_id is False


def test_settings_reject_invalid_port() -> None:
    with pytest.raises(ValidationError):
        Settings(port=0, _env_file=None)


def test_request_id_header_is_normalized() -> None:
    settings = Settings(request_id_header="X-Gateway-Request-ID", _env_file=None)

    assert settings.request_id_header == "x-gateway-request-id"
