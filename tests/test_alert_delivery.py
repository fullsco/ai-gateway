import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from gateway import alert_delivery
from gateway.alert_delivery import (
    alert_delivery_loop,
    deliver_one,
    signed_payload,
    validate_destination,
)
from gateway.app import create_app
from gateway.config import Settings


def settings(**overrides):
    values = {
        "_env_file": None, "database_url": None, "health_probe_enabled": False,
        "alert_monitor_enabled": False,
        "alert_delivery_enabled": True,
        "alert_delivery_url": SecretStr("https://alerts.example.com/hook"),
        "alert_delivery_secret": SecretStr("k" * 32),
    }
    values.update(overrides)
    return Settings(**values)


def row(**overrides):
    values = {
        "id": "11111111-1111-1111-1111-111111111111", "alert_id": 42, "sequence": 1,
        "status": "open", "previous_status": None, "severity": "warning",
        "occurred_at": __import__("datetime").datetime(
            2026, 1, 1, tzinfo=__import__("datetime").timezone.utc),
        "attempts": 1, "lease_token": "22222222-2222-2222-2222-222222222222",
    }
    values.update(overrides)
    return values


def test_default_disabled_and_safe_repr():
    config = Settings(_env_file=None, database_url=None)
    assert config.alert_delivery_enabled is False
    assert config.alert_delivery_url is None
    assert "https" not in repr(config.alert_delivery_url)
    assert "secret" not in repr(config.alert_delivery_secret).lower()


@pytest.mark.parametrize("value", [
    "http://alerts.example.com/hook", "https://user:pass@example.com/h",
    "https://example.com/h#frag", "https://example.com:8443/h",
    "https://192.168.1.1/hook", "https://127.0.0.1/hook", "https://169.254.169.254/hook",
    "https://[::1]/hook", "https://metadata.google.internal/h", "https:// box.example/h",
    "https://exa mple.com/h", "https://example.com/h\n", "https://a" * 300 + ".com/x",
])
def test_invalid_destinations_rejected(value):
    with pytest.raises(ValueError):
        validate_destination(value)


def test_valid_destination_accepted():
    assert validate_destination("https://alerts.example.com/hook") == (
        "https://alerts.example.com/hook")


def test_enabled_requires_url_and_secret():
    with pytest.raises(ValidationError):
        settings(alert_delivery_url=None)
    with pytest.raises(ValidationError):
        settings(alert_delivery_secret=None)
    with pytest.raises(ValidationError):
        settings(alert_delivery_secret=SecretStr("short"))


def test_invalid_url_at_construction_rejected():
    with pytest.raises(ValidationError):
        settings(alert_delivery_url=SecretStr("http://example.com/hook"))


def test_payload_minimal_and_signature_verifies():
    body, headers = signed_payload(row(), "k" * 32)
    decoded = json.loads(body)
    assert set(decoded) == {
        "version", "event_id", "alert_id", "sequence", "status", "previous_status",
        "severity", "occurred_at",
    }
    assert decoded["status"] == "open" and "metadata" not in body.decode()
    expected = __import__("hmac").new(
        ("k" * 32).encode(),
        headers["X-Gateway-Timestamp"].encode() + b"." + body, __import__("hashlib").sha256,
    ).hexdigest()
    assert headers["X-Gateway-Signature"] == "sha256=" + expected
    assert headers["X-Gateway-Event-ID"] == "11111111-1111-1111-1111-111111111111"


@pytest.mark.asyncio
async def test_disabled_loop_never_starts():
    assert await alert_delivery_loop(AsyncMock(), settings(alert_delivery_enabled=False)) is None


@pytest.mark.asyncio
async def test_deliver_one_success_and_headers():
    body, headers = signed_payload(row(), "k" * 32)
    captured = {}

    def handler(request):
        captured.update(url=str(request.url), headers=dict(request.headers))
        return httpx.Response(200)

    store = AsyncMock()
    store.claim.return_value = row()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), trust_env=False,
    ) as client:
        assert await deliver_one(store, client, settings()) is True
    assert captured["url"] == "https://alerts.example.com/hook"
    assert captured["headers"]["x-gateway-event-id"] == str(row()["id"])
    store.finish.assert_awaited_once_with(
        row()["id"], row()["lease_token"], outcome="delivered",
        delay_seconds=0, error=None, status_code=200,
    )


@pytest.mark.asyncio
async def test_deliver_one_transient_and_exhaustion():
    requests = []

    def handler(request):
        requests.append(1)
        return httpx.Response(503)

    store = AsyncMock()
    store.claim.return_value = row(attempts=8)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await deliver_one(store, client, settings(alert_delivery_max_attempts=8))
    assert store.finish.await_args.kwargs["outcome"] == "dead"
    assert store.finish.await_args.kwargs["error"] == "http_transient"


@pytest.mark.asyncio
async def test_deliver_one_permanent_dead():
    async def handler(request):
        return httpx.Response(404)

    store = AsyncMock()
    store.claim.return_value = row()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await deliver_one(store, client, settings())
    assert store.finish.await_args.kwargs["outcome"] == "dead"
    assert store.finish.await_args.kwargs["error"] == "http_permanent"


@pytest.mark.asyncio
async def test_deliver_one_transport_error_retries_with_backoff():
    async def handler(request):
        raise httpx.ConnectError("boom")

    store = AsyncMock()
    store.claim.return_value = row(attempts=2)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await deliver_one(store, client, settings())
    kwargs = store.finish.await_args.kwargs
    assert kwargs["outcome"] == "pending" and kwargs["error"] == "transport_error"
    assert kwargs["delay_seconds"] == 10


@pytest.mark.asyncio
async def test_deliver_one_no_claim_returns_false():
    store = AsyncMock()
    store.claim.return_value = None
    async with httpx.AsyncClient() as client:
        assert await deliver_one(store, client, settings()) is False


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore::RuntimeWarning")
async def test_loop_cancellation_closes_client():
    store = AsyncMock()
    store.claim.return_value = None
    task = asyncio.create_task(alert_delivery_loop(store, settings(alert_delivery_poll_seconds=1)))
    await asyncio.sleep(.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    store.claim.assert_not_called()


@pytest.mark.asyncio
async def test_loop_iteration_failure_continues(monkeypatch):
    attempts = []

    async def failing(store, client, config):
        attempts.append(1)
        raise RuntimeError("boom")

    monkeypatch.setattr(alert_delivery, "deliver_one", failing)
    sleeps = []

    async def sleep(interval):
        sleeps.append(interval)
        if len(sleeps) >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(alert_delivery.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await alert_delivery_loop(AsyncMock(), settings(alert_delivery_poll_seconds=1))
    assert len(attempts) == 2


@pytest.mark.asyncio
async def test_start_alert_delivery_requires_pool_and_schema():
    resources = SimpleNamespace(task=AsyncMock())
    with pytest.raises(RuntimeError):
        await alert_delivery.start_alert_delivery(resources, None, settings())


@pytest.mark.asyncio
async def test_start_alert_delivery_registers_worker(monkeypatch):
    store = AsyncMock()
    store.require_schema.return_value = None
    monkeypatch.setattr(
        alert_delivery, "AlertDeliveryStore", lambda pool: store,
    )
    created = []
    resources = SimpleNamespace(task=lambda coro: created.append(coro))
    await alert_delivery.start_alert_delivery(resources, AsyncMock(), settings())
    assert len(created) == 1
    for coro in created:
        coro.close()


def test_app_lifecycle_mounts_delivery(monkeypatch):
    started = {}

    async def fake_start(resources, pool, config):
        started["pool"] = pool
        started["config"] = config

    monkeypatch.setattr(alert_delivery, "start_alert_delivery", fake_start)
    app = create_app(settings())
    app.state.lifecycle_resources = SimpleNamespace(task=AsyncMock())
    assert started.get("config") is None or started["config"].alert_delivery_enabled
    assert app.state.settings.alert_delivery_enabled
