import json
import logging

from gateway.logging import REDACTED, GatewayFormatter, redact


def test_redact_removes_nested_secrets() -> None:
    value = {
        "authorization": "Bearer secret",
        "nested": {"api_key": "secret", "safe": "visible"},
        "items": [{"access_token": "secret"}],
    }

    assert redact(value) == {
        "authorization": REDACTED,
        "nested": {"api_key": REDACTED, "safe": "visible"},
        "items": [{"access_token": REDACTED}],
    }


def test_json_formatter_redacts_structured_fields() -> None:
    record = logging.LogRecord("test", logging.INFO, "", 0, "event", (), None)
    record.fields = {"x-api-key": "secret", "provider": "example"}

    output = json.loads(GatewayFormatter("json").format(record))

    assert output["x-api-key"] == REDACTED
    assert output["provider"] == "example"
