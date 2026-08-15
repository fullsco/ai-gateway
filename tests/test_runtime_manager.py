import base64
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
