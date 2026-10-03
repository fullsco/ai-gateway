"""Exercise persisted runtime refreshes inside the accounting fixture's rollback."""

import base64
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from gateway.configuration import (
    CachedConfiguration,
    ConfigSnapshot,
    PostgresSnapshotRepository,
    RuntimeBuilder,
    RuntimeManager,
)
from gateway.security import CredentialCipher
from tests.test_accounting_integration import TransactionPool, fixture

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ENCRYPTION_KEY = base64.b64encode(b"e" * 32).decode("ascii")
KEY_PEPPER = base64.b64encode(b"p" * 32).decode("ascii")
ROUTE_ID = "synthetic-lifecycle-route"
CREDENTIAL_ID = "synthetic-lifecycle-credential"


def payload(revision: str) -> dict:
    cipher = CredentialCipher.from_base64(ENCRYPTION_KEY)
    envelope = cipher.encrypt(
        f"synthetic-secret-{revision}", context=f"provider-credential:{CREDENTIAL_ID}",
    )
    return {
        "clients": [{
            "id": "synthetic-client", "name": "Runtime lifecycle test",
            "allowed_protocols": ["anthropic_messages"],
            "requests_per_minute": 10, "tokens_per_minute": 1000, "spending_limit": 5,
        }],
        "gateway_keys": [{
            "id": "synthetic-key", "client_id": "synthetic-client",
            "key_prefix": "gw_live_examplex", "key_digest": "0" * 64,
        }],
        "providers": [{
            "id": "synthetic-provider", "name": "Synthetic provider",
            "provider_type": "anthropic_compatible", "protocol": "anthropic_messages",
            "base_url": "https://provider.invalid", "capabilities": ["streaming"],
        }],
        "credentials": [{
            "id": CREDENTIAL_ID, "provider_id": "synthetic-provider",
            "secret_version": 1, "secret_nonce": envelope.nonce,
            "secret_ciphertext": envelope.ciphertext,
            "quota_limit": 10, "quota_used": 2,
        }],
        "models": [{
            "id": "synthetic-model", "aliases": ["latest"], "capabilities": ["streaming"],
        }],
        "provider_models": [{
            "id": ROUTE_ID, "canonical_model_id": "synthetic-model",
            "provider_id": "synthetic-provider", "upstream_model_id": f"upstream-{revision}",
            "protocol": "anthropic_messages", "capabilities": ["streaming"],
            "max_concurrency": 1,
        }],
    }


async def publish(db, revision: str) -> ConfigSnapshot:
    assert db.is_in_transaction(), "Synthetic publication must be rolled back"
    # Serialize version allocation without advancing the non-transactional identity
    # sequence. The fixture bounds lock waits and rolls back statuses and inserts.
    await db.execute("lock table public.config_versions in share row exclusive mode")
    version = await db.fetchval("select coalesce(max(id), 0) + 1 from public.config_versions")
    snapshot = ConfigSnapshot.create(
        version=version, payload=payload(revision), published_at=datetime.now(UTC),
    )
    await db.execute(
        "update public.config_versions set status='superseded' where status='published'",
    )
    await db.execute(
        "insert into public.config_versions "
        "(id,status,schema_version,payload,checksum,published_at) overriding system value "
        "values ($1,'published',$2,$3::jsonb,$4,$5)",
        snapshot.version, snapshot.schema_version, json.dumps(snapshot.payload),
        snapshot.checksum, snapshot.published_at,
    )
    return snapshot


async def test_published_runtime_preserves_live_lease_and_recovers_after_repository_outage(
    monkeypatch,
):
    def reject_request(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Runtime lifecycle test must not dispatch HTTP requests")

    async with fixture() as (_, db, _, _):
        pool = TransactionPool(db)
        repository = PostgresSnapshotRepository(pool)
        cache = CachedConfiguration(repository)
        builder = RuntimeBuilder(encryption_key=ENCRYPTION_KEY, key_pepper=KEY_PEPPER)
        manager = RuntimeManager(cache, builder)
        # Replacing only the transport factory avoids loading application settings
        # or proxy credentials; repository, cache, builder and manager stay real.
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(reject_request), trust_env=False,
        ) as transport:
            factory = Mock(return_value=transport)
            monkeypatch.setattr(
                "gateway.configuration.runtime_builder.build_upstream_client", factory,
            )
            leases = []
            try:
                first_snapshot = await publish(db, "first")
                old = await manager.refresh()
                assert old is not None
                assert manager.version == first_snapshot.version
                assert cache.snapshot == first_snapshot
                assert cache.snapshot.verify_checksum()
                assert old.http_client is transport
                assert old.credentials[CREDENTIAL_ID].secret == "synthetic-secret-first"
                assert old.model_registry.resolve("latest").id == "synthetic-model"
                assert ROUTE_ID in old.provider_model_adapters
                client = old.key_store.get_client("synthetic-client")
                assert client is not None and client.requests_per_minute == 10

                controls = old.route_controls
                held = await controls.acquire(ROUTE_ID, timeout_seconds=.1)
                assert held is not None
                leases.append(held)
                await controls.record_failure(ROUTE_ID)
                circuit = controls.circuit_snapshot(ROUTE_ID)

                second_snapshot = await publish(db, "second")
                current = await manager.refresh()
                assert current is not None and current is not old
                assert manager.runtime is current
                assert manager.version == second_snapshot.version > first_snapshot.version
                assert cache.snapshot == second_snapshot
                assert current.credentials[CREDENTIAL_ID].secret == "synthetic-secret-second"
                assert old.credentials[CREDENTIAL_ID].secret == "synthetic-secret-first"
                assert old.model_registry.list_provider_models()[0].upstream_model_id == (
                    "upstream-first"
                )
                assert current.model_registry.list_provider_models()[0].upstream_model_id == (
                    "upstream-second"
                )
                assert current.http_client is old.http_client is transport
                assert not transport.is_closed
                assert current.route_controls is controls
                assert controls.circuit_snapshot(ROUTE_ID) is circuit
                assert await current.route_controls.acquire(ROUTE_ID, timeout_seconds=.01) is None
                assert await manager.refresh() is current

                # Publish while the old lease remains held. A repository read failure
                # must keep the last good runtime even though a newer row now exists.
                third_snapshot = await publish(db, "recovered")
                last_good = cache.snapshot
                outage = AsyncMock(side_effect=ConnectionError("synthetic repository outage"))
                with monkeypatch.context() as patch:
                    patch.setattr(pool, "fetchrow", outage)
                    assert await manager.refresh() is current
                    outage.assert_awaited_once()
                assert manager.runtime is current
                assert manager.version == second_snapshot.version
                assert cache.snapshot is last_good
                assert cache.last_refresh_error == "ConnectionError"
                assert current.http_client is transport and not transport.is_closed
                assert current.route_controls is controls
                assert await controls.acquire(ROUTE_ID, timeout_seconds=.01) is None

                recovered = await manager.refresh()
                assert recovered is not None and recovered is not current and recovered is not old
                assert manager.runtime is recovered
                assert manager.version == third_snapshot.version > second_snapshot.version
                assert cache.snapshot == third_snapshot
                assert cache.snapshot.verify_checksum()
                assert cache.last_refresh_error is None
                assert recovered.credentials[CREDENTIAL_ID].secret == "synthetic-secret-recovered"
                assert recovered.model_registry.list_provider_models()[0].upstream_model_id == (
                    "upstream-recovered"
                )
                assert recovered.http_client is transport and not transport.is_closed
                assert recovered.route_controls is controls
                assert controls.circuit_snapshot(ROUTE_ID) is circuit
                assert await recovered.route_controls.acquire(ROUTE_ID, timeout_seconds=.01) is None

                held.release()
                held.release()  # An old stream's repeated finalization cannot add capacity.
                replacement = await recovered.route_controls.acquire(ROUTE_ID, timeout_seconds=.1)
                assert replacement is not None
                leases.append(replacement)
                assert await old.route_controls.acquire(ROUTE_ID, timeout_seconds=.01) is None
                replacement.release()
                assert await manager.refresh() is recovered
                factory.assert_called_once_with()
            finally:
                for lease in leases:
                    lease.release()
                await manager.close()
            assert manager.runtime is None
            assert transport.is_closed
