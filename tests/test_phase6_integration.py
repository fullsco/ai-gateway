import os

import pytest

from gateway.configuration import (
    CachedConfiguration,
    ConfigurationUnavailable,
    PostgresSnapshotRepository,
    create_pool,
)

pytestmark = pytest.mark.integration


def _database_url() -> str:
    value = os.environ.get("GATEWAY_DATABASE_URL")
    if not value:
        pytest.skip("GATEWAY_DATABASE_URL is required for PostgreSQL integration tests")
    return value


@pytest.mark.asyncio
async def test_postgres_repository_loads_current_published_snapshot() -> None:
    pool = await create_pool(_database_url())
    try:
        snapshot = await PostgresSnapshotRepository(pool).load_published()
        assert snapshot is not None
        assert snapshot.version >= 1
        assert snapshot.verify_checksum()
        assert snapshot.payload["clients"]
        assert snapshot.payload["provider_models"]
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_cache_retains_live_snapshot_after_postgres_outage() -> None:
    pool = await create_pool(_database_url())
    cache = CachedConfiguration(PostgresSnapshotRepository(pool))
    try:
        loaded = await cache.refresh()
        await pool.close()

        retained = await cache.refresh()

        assert retained.version == loaded.version
        assert retained.payload == loaded.payload
        assert cache.snapshot is retained
        assert cache.last_refresh_error
    finally:
        if not pool.is_closing():
            await pool.close()


@pytest.mark.asyncio
async def test_cache_without_snapshot_fails_closed_when_postgres_is_unavailable() -> None:
    pool = await create_pool(_database_url())
    cache = CachedConfiguration(PostgresSnapshotRepository(pool))
    await pool.close()

    with pytest.raises(ConfigurationUnavailable, match="no cached snapshot"):
        await cache.refresh()
