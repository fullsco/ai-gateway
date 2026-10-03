import json
import os

import pytest

from gateway.configuration import (
    CachedConfiguration,
    ConfigurationUnavailable,
    PostgresSnapshotRepository,
    configuration_checksum,
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


@pytest.mark.asyncio
async def test_snapshot_refresh_tracks_synthetic_publications_with_rollback() -> None:
    """Exercise publication/cache changes without exposing test config to other sessions."""
    pool = await create_pool(_database_url())
    try:
        async with pool.acquire() as connection:
            transaction = connection.transaction()
            await transaction.start()
            try:
                await connection.execute("set local lock_timeout = '3s'")
                await connection.execute(
                    "update public.config_versions set status='superseded' where status='published'"
                )
                repository = PostgresSnapshotRepository(connection)
                cache = CachedConfiguration(repository)
                previous = None
                for label in ("first", "second"):
                    payload = {
                        "name": f"synthetic-dev-{label}", "clients": [], "provider_models": [],
                    }
                    await connection.execute(
                        "update public.config_versions set status='superseded' "
                        "where status='published'"
                    )
                    version = await connection.fetchval(
                        "insert into public.config_versions "
                        "(status, schema_version, payload, checksum, published_at) "
                        "values ('published', 1, $1::jsonb, $2, now()) returning id",
                        json.dumps(payload), configuration_checksum(payload),
                    )
                    snapshot = await cache.refresh()
                    assert snapshot.version == version
                    assert snapshot.payload == payload
                    assert snapshot.verify_checksum()
                    assert snapshot is not previous
                    assert await cache.refresh() is snapshot
                    previous = snapshot
            finally:
                await transaction.rollback()
    finally:
        await pool.close()
