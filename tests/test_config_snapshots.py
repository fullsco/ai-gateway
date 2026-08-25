from datetime import UTC, datetime
from typing import Any

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


@pytest.mark.asyncio
async def test_a_hung_snapshot_load_cannot_freeze_configuration_forever() -> None:
    """A load that never returns must time out, not hold the lock indefinitely.

    refresh serialises on a lock, so one hung database connection blocks every later
    refresh behind it. That happened in production: a DNS outage left a connection
    hanging, the running configuration froze at version 245, and the gateway served it
    for hours while the dashboard correctly reported 248 as published. Nothing logged,
    because an await that never returns is not an error.
    """
    import asyncio

    from gateway.configuration.snapshots import CachedConfiguration

    class HangingRepository:
        def __init__(self, first: ConfigSnapshot) -> None:
            self._first = first
            self.calls = 0

        async def load_published(self):
            self.calls += 1
            if self.calls == 1:
                return self._first
            await asyncio.sleep(3600)  # never returns

    good = snapshot(version=245)
    repository = HangingRepository(good)
    cache = CachedConfiguration(repository)
    cache.LOAD_TIMEOUT_SECONDS = 0.05

    assert (await cache.refresh()).version == 245

    # The hung load must give up and fall back to the cached snapshot, and the
    # failure must be recorded rather than swallowed.
    served = await asyncio.wait_for(cache.refresh(), timeout=5)
    assert served.version == 245
    assert cache.last_refresh_error == "TimeoutError"

    # And crucially, a later refresh must not be stuck behind the abandoned one.
    served = await asyncio.wait_for(cache.refresh(), timeout=5)
    assert served.version == 245
    assert repository.calls == 3


@pytest.mark.asyncio
async def test_serving_the_cached_snapshot_is_reported_not_silent() -> None:
    """Serving stale configuration is correct during an outage; hiding it is not."""
    from gateway.configuration.snapshots import CachedConfiguration

    class FailingRepository:
        def __init__(self, first: ConfigSnapshot) -> None:
            self._first = first
            self.calls = 0

        async def load_published(self):
            self.calls += 1
            if self.calls == 1:
                return self._first
            raise OSError("name resolution failed")

    cache = CachedConfiguration(FailingRepository(snapshot(version=245)))
    assert (await cache.refresh()).version == 245
    assert cache.last_refresh_error is None

    served = await cache.refresh()
    assert served.version == 245, "the cached snapshot must still be served"
    assert cache.last_refresh_error == "OSError", "the reason must be retained"


def test_a_field_from_a_newer_publisher_does_not_reject_the_snapshot() -> None:
    """An older build must keep running when configuration gains a field.

    This is the exact production failure. Adding timeout_seconds to provider_models
    made every older instance reject the whole payload, because the snapshot models
    forbade unknown fields. One instance sat on version 242 for a day while 249 was
    published, logging fifteen thousand validation errors and changing nothing. A
    reader that is older than its publisher has to degrade, not stop: it should run
    without the feature it cannot understand.
    """
    from gateway.configuration.runtime_builder import (
        RuntimeSnapshot,
        unknown_snapshot_fields,
    )

    base = {
        "clients": [],
        "gateway_keys": [],
        "providers": [
            {
                "id": "p",
                "name": "P",
                "base_url": "https://upstream.example",
                "capabilities": [],
            }
        ],
        "credentials": [],
        "models": [{"id": "m", "capabilities": []}],
        "provider_models": [
            {
                "id": "pm",
                "canonical_model_id": "m",
                "provider_id": "p",
                "upstream_model_id": "u",
                "protocol": "anthropic_messages",
                "capabilities": [],
            }
        ],
    }

    # Sanity: the payload this build understands validates.
    assert RuntimeSnapshot.model_validate(base) is not None
    assert unknown_snapshot_fields(base) == {}

    # Now the publisher adds a field on a row, and a whole section, from the future.
    future = {
        **base,
        "provider_models": [{**base["provider_models"][0], "a_new_knob": 42}],
        "sections_added_later": [{"anything": True}],
    }
    snapshot = RuntimeSnapshot.model_validate(future)
    assert snapshot is not None, "an older build must still load the snapshot"
    assert snapshot.provider_models[0].id == "pm"

    # And the gap must be reported, so running without the field is visible.
    assert unknown_snapshot_fields(future) == {
        "provider_models": ["a_new_knob"],
        "snapshot": ["sections_added_later"],
    }


def test_a_snapshot_that_is_genuinely_broken_is_still_rejected() -> None:
    """Tolerating unknown fields must not tolerate a payload that cannot work."""
    from gateway.configuration.runtime_builder import RuntimeSnapshot

    dangling = {
        "clients": [],
        "gateway_keys": [],
        "providers": [],
        "credentials": [],
        "models": [],
        "provider_models": [
            {
                "id": "pm",
                "canonical_model_id": "missing-model",
                "provider_id": "missing-provider",
                "upstream_model_id": "u",
                "protocol": "anthropic_messages",
                "capabilities": [],
            }
        ],
    }
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="unknown provider"):
        RuntimeSnapshot.model_validate(dangling)


def test_a_changed_routing_policy_is_described() -> None:
    """Routing weights decide which provider serves a request, so a publish must say.

    The policy is attached to the route in the snapshot, and changing it changes what
    the gateway does, yet nothing described it: the section reported as changed with
    nothing listed under it.
    """
    def payload(policy: Any) -> dict[str, Any]:
        return {
            "providers": [{"id": "p1", "name": "OpenRouter"}],
            "provider_models": [
                {
                    "id": "pm1",
                    "provider_id": "p1",
                    "canonical_model_id": "stealth/ox-alpha",
                    "upstream_model_id": "stealth/ox-alpha",
                    "routing_policy": policy,
                }
            ],
        }

    changes = summarize_configuration_changes(
        payload(None), payload({"health_weight": 3, "latency_weight": 1})
    )

    assert changes, "a routing policy change produced no description"
    assert "routing policy" in " ".join(entry["summary"] for entry in changes).lower()


def test_every_difference_is_described_by_something() -> None:
    """If the gateway says configuration changed, it must be able to name a change.

    Whether anything changed is decided by comparing whole projections, while what
    changed is described by per-field tables. The two disagreed whenever a field was
    added to the payload but not to a table, and the operator was shown "you have
    unpublished changes" above an empty list, with no way to see what would be
    published or to get back to a clean state. Publishing was the only way out, which
    is exactly the action nobody should take blind.

    This asserts the invariant rather than a field: any projection difference yields
    at least one entry, including for fields that do not exist yet.
    """
    published = {
        "providers": [{"id": "p1", "name": "OpenRouter"}],
        "provider_models": [{"id": "pm1", "provider_id": "p1", "upstream_model_id": "m"}],
    }
    working = {
        "providers": [{"id": "p1", "name": "OpenRouter"}],
        "provider_models": [
            {
                "id": "pm1",
                "provider_id": "p1",
                "upstream_model_id": "m",
                # A field no summary table knows about, standing in for the next one
                # somebody adds to the snapshot.
                "some_future_field": {"enabled": True},
            }
        ],
    }

    assert configuration_checksum(published) != configuration_checksum(working)
    changes = summarize_configuration_changes(published, working)
    assert changes, "projections differ but nothing was described"
