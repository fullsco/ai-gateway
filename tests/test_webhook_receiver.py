import hashlib
import hmac
import json
import time

import httpx
import pytest

from scripts.staging_boundary import clean_environment, validate_dsn
from scripts.webhook_receiver import URL, LoopbackTLS, Receiver

SECRET = "synthetic-webhook-receiver-secret-32"


def signed(*, timestamp=None, **changes):
    payload = {"version": 1, "event_id": "fixture-event", "alert_id": "fixture-alert",
               "sequence": 1, "status": "open", "previous_status": None,
               "severity": "warning", "occurred_at": "2026-09-18T00:00:00+00:00", **changes}
    body = json.dumps(payload).encode()
    timestamp = str(int(time.time()) if timestamp is None else timestamp)
    signature = hmac.new(SECRET.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    return body, {"x-gateway-event-id": payload["event_id"], "x-gateway-timestamp": timestamp,
                  "x-gateway-signature": "sha256=" + signature.hexdigest()}


def test_receiver_retries_and_deduplicates_only_identical_signed_events():
    receiver = Receiver(SECRET)
    body, headers = signed()
    assert receiver.receive(body, headers) == 503
    assert receiver.receive(body, headers) == 204
    assert receiver.receive(body, headers) == 204
    assert receiver.duplicates == 1 and len(receiver.accepted) == 1
    assert receiver.receive(*signed(sequence=2)) == 409


@pytest.mark.parametrize("case", ["tamper", "expired", "event_id", "metadata", "signature"])
def test_receiver_rejects_untrusted_or_excess_payloads(case):
    receiver = Receiver(SECRET)
    body, headers = signed(**({"metadata": "private"} if case == "metadata" else {}),
                           timestamp=0 if case == "expired" else None)
    if case == "tamper":
        body = body.replace(b"warning", b"critical")
    if case == "event_id":
        headers["x-gateway-event-id"] = "another-event"
    if case == "signature":
        headers["x-gateway-signature"] = "sha256=invalid"
    assert receiver.receive(body, headers) == 401
    assert not receiver.accepted and not receiver.attempts


@pytest.mark.asyncio
async def test_loopback_transport_rejects_external_destinations():
    transport = LoopbackTLS.__new__(LoopbackTLS)
    for url in ("https://example.com/alerts", URL + "/other", URL.replace("https", "http")):
        with pytest.raises(RuntimeError, match="rejects"):
            await transport.handle_async_request(httpx.Request("POST", url))


@pytest.mark.parametrize("dsn", [
    "postgresql://gateway_qualification:fixture@127.0.0.1:5432/production",
    "postgresql://gateway_qualification:fixture@db.example.com:5432/gateway_qualification",
    "postgresql://gateway_qualification:fixture@127.0.0.1:5432/gateway_qualification?host=other",
    "postgresql://postgres:fixture@127.0.0.1:5432/gateway_qualification",
    "postgresql://gateway_qualification:fixture@127.0.0.1:6543/gateway_qualification",
])
def test_drill_rejects_other_databases(dsn):
    with pytest.raises(ValueError, match="isolated loopback"):
        validate_dsn(dsn)


def test_child_environment_discards_inherited_gateway_and_proxy_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("GATEWAY_DATABASE_URL", "must-not-inherit")
    monkeypatch.setenv("HTTPS_PROXY", "must-not-inherit")
    env = clean_environment(tmp_path)
    assert set(env) == {"PATH", "HOME", "LANG", "PYTHONNOUSERSITE"}
