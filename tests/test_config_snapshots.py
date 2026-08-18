from datetime import UTC, datetime

import pytest

from gateway.configuration import (
    CachedConfiguration,
    ConfigSnapshot,
    ConfigurationUnavailable,
    configuration_checksum,
    legacy_configuration_checksum,
    summarize_configuration_changes,
)


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


def test_configuration_checksum_ignores_runtime_state_and_collection_order() -> None:
    first = {
        "providers": [{"id": "b", "health": "healthy"}, {"id": "a"}],
        "credentials": [{"id": "two", "quota_used": 1}, {"id": "one"}],
    }
    second = {
        "providers": [{"id": "a"}, {"id": "b", "health": "unavailable"}],
        "credentials": [{"id": "one"}, {"id": "two", "quota_used": 99}],
    }

    assert configuration_checksum(first) == configuration_checksum(second)
    assert legacy_configuration_checksum(first) != legacy_configuration_checksum(second)


def test_summarize_changes_describes_additions_removals_and_updates() -> None:
    published = {
        "providers": [
            {"id": "p1", "name": "AgentRouter", "enabled": True, "priority": 100},
            {"id": "p2", "name": "LegacyRouter", "enabled": True, "priority": 100},
        ],
        "models": [{"id": "claude-opus-5", "enabled": True, "capabilities": ["streaming"]}],
        "provider_models": [
            {
                "id": "pm1",
                "canonical_model_id": "claude-opus-5",
                "provider_id": "p1",
                "priority": 100,
                "enabled": True,
            }
        ],
    }
    working = {
        "providers": [
            {"id": "p1", "name": "AgentRouter", "enabled": True, "priority": 100},
            {"id": "p3", "name": "GoRouter", "enabled": True, "priority": 100},
        ],
        "models": [{"id": "claude-opus-5", "enabled": True, "capabilities": ["streaming"]}],
        "provider_models": [
            {
                "id": "pm1",
                "canonical_model_id": "claude-opus-5",
                "provider_id": "p1",
                "priority": 10,
                "enabled": True,
            },
            {
                "id": "pm2",
                "canonical_model_id": "claude-opus-5",
                "provider_id": "p3",
                "priority": 20,
                "enabled": True,
            },
        ],
    }

    summaries = [entry["summary"] for entry in summarize_configuration_changes(published, working)]

    assert "Added provider GoRouter (enabled)" in summaries
    assert "Removed provider LegacyRouter" in summaries
    assert "Added route: claude-opus-5 via GoRouter" in summaries
    assert "Changed route claude-opus-5 via AgentRouter: priority 100 to 10" in summaries


def test_summarize_changes_never_exposes_secrets_or_digests() -> None:
    published = {
        "credentials": [
            {
                "id": "c1",
                "provider_id": "p1",
                "enabled": True,
                "secret_ciphertext": "PUBLISHED-SECRET",
            }
        ],
        "providers": [{"id": "p1", "name": "AgentRouter"}],
        "gateway_keys": [
            {
                "id": "k1",
                "key_prefix": "gw_live",
                "key_digest": "PUBLISHED-DIGEST",
                "enabled": True,
            }
        ],
    }
    working = {
        "credentials": [
            {
                "id": "c1",
                "provider_id": "p1",
                "enabled": False,
                "secret_ciphertext": "WORKING-SECRET",
            },
            {
                "id": "c2",
                "provider_id": "p1",
                "enabled": True,
                "secret_ciphertext": "NEW-SECRET",
            },
        ],
        "providers": [{"id": "p1", "name": "AgentRouter"}],
        "gateway_keys": [],
    }

    changes = summarize_configuration_changes(published, working)
    text = " ".join(entry["summary"] for entry in changes)

    assert "SECRET" not in text
    assert "DIGEST" not in text
    assert "Added 1 credential to AgentRouter" in text
    assert "Disabled a credential on AgentRouter" in text
    assert "Revoked key gw_live" in text


def test_summarize_changes_is_empty_for_identical_payloads() -> None:
    payload = {"providers": [{"id": "p1", "name": "AgentRouter", "health": "healthy"}]}
    assert summarize_configuration_changes(payload, payload) == []
