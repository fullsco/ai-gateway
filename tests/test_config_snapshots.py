from datetime import UTC, datetime

import pytest

from gateway.configuration import CachedConfiguration, ConfigSnapshot, ConfigurationUnavailable


class StubRepository:
    def __init__(self, results):
        self.results = iter(results)

    async def load_published(self):
        result = next(self.results)
        if isinstance(result, Exception):
            raise result
        return result


def snapshot(version: int, name: str = "config") -> ConfigSnapshot:
    return ConfigSnapshot.create(
        version=version,
        payload={"name": name},
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_refresh_loads_and_atomically_replaces_newer_snapshot() -> None:
    cache = CachedConfiguration(StubRepository([snapshot(1), snapshot(2)]))

    first = await cache.refresh()
    second = await cache.refresh()

    assert first.version == 1
    assert second.version == 2
    assert cache.last_refresh_error is None


@pytest.mark.asyncio
async def test_database_failure_keeps_last_known_good_snapshot() -> None:
    cache = CachedConfiguration(StubRepository([snapshot(1), RuntimeError("database down")]))

    await cache.refresh()
    retained = await cache.refresh()

    assert retained.version == 1
    assert cache.last_refresh_error == "RuntimeError"


@pytest.mark.asyncio
async def test_initial_database_failure_marks_configuration_unavailable() -> None:
    cache = CachedConfiguration(StubRepository([RuntimeError("database down")]))

    with pytest.raises(ConfigurationUnavailable, match="no cached snapshot"):
        await cache.refresh()


@pytest.mark.asyncio
async def test_invalid_checksum_never_replaces_valid_snapshot() -> None:
    valid = snapshot(1)
    invalid = valid.model_copy(update={"version": 2, "checksum": "0" * 64})
    cache = CachedConfiguration(StubRepository([valid, invalid]))

    await cache.refresh()
    retained = await cache.refresh()

    assert retained.version == 1
    assert cache.last_refresh_error == "ConfigurationUnavailable"


@pytest.mark.asyncio
async def test_older_snapshot_does_not_roll_configuration_back() -> None:
    cache = CachedConfiguration(StubRepository([snapshot(2), snapshot(1)]))

    await cache.refresh()
    retained = await cache.refresh()

    assert retained.version == 2
