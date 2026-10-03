import base64
import gc
import weakref
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from gateway.configuration import (
    CachedConfiguration,
    ConfigSnapshot,
    RuntimeBuilder,
    RuntimeManager,
)
from gateway.security import CredentialCipher


class StubRepository:
    def __init__(self, snapshots):
        self._snapshots = iter(snapshots)

    async def load_published(self):
        item = next(self._snapshots)
        if isinstance(item, Exception):
            raise item
        return item


def encoded(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def payload(secret: str = "first") -> tuple[RuntimeBuilder, dict]:
    encryption_key = encoded(b"e" * 32)
    cipher = CredentialCipher.from_base64(encryption_key)
    envelope = cipher.encrypt(secret, context="provider-credential:credential")
    return RuntimeBuilder(encryption_key=encryption_key, key_pepper=encoded(b"p" * 32)), {
        "clients": [],
        "gateway_keys": [],
        "providers": [],
        "credentials": [
            {
                "id": "credential",
                "provider_id": "provider",
                "secret_nonce": envelope.nonce,
                "secret_ciphertext": envelope.ciphertext,
            }
        ],
        "models": [],
        "provider_models": [],
    }


def snapshot(version: int, body: dict) -> ConfigSnapshot:
    return ConfigSnapshot.create(
        version=version,
        payload=body,
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_manager_swaps_newer_runtime_and_defers_old_client_close() -> None:
    builder, first_payload = payload("first")
    _, second_payload = payload("second")
    manager = RuntimeManager(
        CachedConfiguration(
            StubRepository([snapshot(1, first_payload), snapshot(2, second_payload)])
        ),
        builder,
    )

    first = await manager.refresh()
    assert first is not None
    first_client = first.http_client
    second = await manager.refresh()

    assert second is not first
    assert second.credentials["credential"].secret == "second"
    assert first_client is not None and not first_client.is_closed
    assert second.http_client is first_client
    assert second.route_controls is first.route_controls
    assert manager._retired_runtimes == []
    await manager.close()
    assert first_client.is_closed
    assert second.http_client is not None and second.http_client.is_closed


@pytest.mark.asyncio
async def test_manager_keeps_runtime_when_refresh_fails() -> None:
    builder, body = payload()
    manager = RuntimeManager(
        CachedConfiguration(StubRepository([snapshot(1, body), RuntimeError("database down")])),
        builder,
    )

    first = await manager.refresh()
    retained = await manager.refresh()

    assert retained is first
    await manager.close()


@pytest.mark.asyncio
async def test_manager_recovers_when_first_snapshot_is_published_later() -> None:
    builder, body = payload()
    manager = RuntimeManager(
        CachedConfiguration(
            StubRepository([None, snapshot(1, body)])
        ),
        builder,
    )

    with pytest.raises(RuntimeError):
        await manager.refresh()

    runtime = await manager.refresh()

    assert runtime is not None
    assert manager.version == 1
    await manager.close()


@pytest.mark.asyncio
async def test_manager_activates_rollback_published_as_newer_version() -> None:
    builder, first_payload = payload("first")
    _, second_payload = payload("second")
    _, rollback_payload = payload("first")
    manager = RuntimeManager(
        CachedConfiguration(
            StubRepository(
                [
                    snapshot(3, first_payload),
                    snapshot(7, second_payload),
                    snapshot(8, rollback_payload),
                ]
            )
        ),
        builder,
    )

    await manager.refresh()
    await manager.refresh()
    rolled_back = await manager.refresh()

    assert manager.version == 8
    assert rolled_back is not None
    assert rolled_back.credentials["credential"].secret == "first"
    await manager.close()


@pytest.mark.asyncio
async def test_many_refreshes_reuse_one_transport_and_do_not_accumulate_retired_clients():
    builder, body = payload()
    manager = RuntimeManager(
        CachedConfiguration(StubRepository([snapshot(version, body) for version in range(1, 26)])),
        builder,
    )
    first = await manager.refresh()
    try:
        for _ in range(24):
            current = await manager.refresh()
            assert current.http_client is first.http_client
            assert current.route_controls is first.route_controls
            assert manager._retired_runtimes == []
    finally:
        await manager.close()
    assert first.http_client.is_closed


def route_payload(route_ids: list[str]) -> tuple[RuntimeBuilder, dict]:
    builder, body = payload()
    body["providers"] = [{
        "id": "provider", "name": "Provider", "base_url": "https://provider.invalid",
        "protocol": "openai_chat_completions", "capabilities": ["streaming"],
    }]
    body["models"] = [{"id": "model", "capabilities": ["streaming"]}]
    body["provider_models"] = [{
        "id": route_id, "canonical_model_id": "model", "provider_id": "provider",
        "upstream_model_id": "model", "protocol": "openai_chat_completions",
        "capabilities": ["streaming"], "max_concurrency": 1,
    } for route_id in route_ids]
    return builder, body


@pytest.mark.asyncio
async def test_route_churn_prunes_state_but_preserves_existing_route():
    builder, _ = route_payload([])
    manager = RuntimeManager(CachedConfiguration(StubRepository([
        snapshot(version, route_payload(["stable", f"route-{version}"])[1])
        for version in range(1, 26)
    ])), builder)
    try:
        current = await manager.refresh()
        controls = current.route_controls
        lease = await controls.acquire("stable", timeout_seconds=.1)
        assert lease is not None
        await controls.record_failure("stable")
        circuit = controls.circuit_snapshot("stable")
        for version in range(1, 26):
            if version > 1:
                previous = weakref.ref(current)
                current = await manager.refresh()
                gc.collect()
                assert previous() is None
            await controls.record_failure(f"route-{version}")
            assert set(controls._limits) == {"stable", f"route-{version}"}
            assert set(controls._circuits) == {"stable", f"route-{version}"}
            assert controls.circuit_snapshot("stable") is circuit
            assert controls._active == {"stable": 1}
            assert len(controls._owners) == 2  # Current registry and held lease.
        assert await controls.acquire("stable", timeout_seconds=.01) is None
        lease.release()
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_removed_route_retained_by_old_runtime_then_stream_lease():
    builder, body = route_payload(["removed"])
    _, empty = route_payload([])
    manager = RuntimeManager(
        CachedConfiguration(StubRepository([snapshot(1, body), snapshot(2, empty)])), builder,
    )
    try:
        old = await manager.refresh()
        controls = old.route_controls
        await controls.record_failure("removed")
        circuit = controls.circuit_snapshot("removed")
        await manager.refresh()
        gc.collect()
        assert controls._limits == {"removed": 1}
        assert controls.circuit_snapshot("removed") is circuit
        # An idle old runtime can still start work after the route was removed.
        lease = await old.route_controls.acquire("removed", timeout_seconds=.1)
        assert lease is not None
        reference = weakref.ref(old)
        del old
        gc.collect()
        assert reference() is None
        assert await controls.acquire("removed", timeout_seconds=.01) is None
        assert controls.circuit_snapshot("removed") is circuit
        lease.release()
        lease.release()
        # Stream finalization releases concurrency before recording the outcome.
        assert controls._active == {}
        assert controls._limits == {"removed": 1}
        await controls.record_failure("removed")
        assert controls.circuit_snapshot("removed").consecutive_failures == 2
        del lease
        gc.collect()
        assert controls._limits == {}
        assert controls._circuits == {}
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_replaced_runtime_keeps_removed_routes_after_original_is_collected():
    builder, body = route_payload(["removed"])
    _, empty = route_payload([])
    manager = RuntimeManager(
        CachedConfiguration(StubRepository([snapshot(1, body), snapshot(2, empty)])), builder,
    )
    try:
        original = await manager.refresh()
        # The executor replaces GatewayRuntime when wrapping shared controls.
        copied = replace(original, credentials=dict(original.credentials))
        controls = copied.route_controls
        original_reference = weakref.ref(original)
        del original
        await manager.refresh()
        gc.collect()
        assert original_reference() is None
        assert controls._limits == {"removed": 1}
        lease = await copied.route_controls.acquire("removed", timeout_seconds=.1)
        assert lease is not None
        lease.release()
        del lease
        assert controls._limits == {"removed": 1}
        del copied
        gc.collect()
        assert controls._limits == {}
        assert controls._route_owners == {}
    finally:
        await manager.close()
