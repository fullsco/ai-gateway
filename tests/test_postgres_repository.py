import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from gateway.configuration import (
    CachedConfiguration,
    PostgresSnapshotRepository,
    configuration_checksum,
    create_pool,
)


class FakePool:
    """Mock asyncpg Pool that records executed SQL queries and yields programmed rows."""

    def __init__(self, responses: list[dict[str, Any] | None]) -> None:
        self.responses = responses
        self.queries: list[tuple[str, tuple[Any, ...]]] = []
        self._index = 0

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.queries.append((query.strip(), args))
        if self._index < len(self.responses):
            res = self.responses[self._index]
            self._index += 1
            return res
        return None


def make_payload(name: str = "default") -> dict[str, Any]:
    return {
        "name": name,
        "clients": [],
        "providers": [{"id": "p1", "name": "AgentRouter", "enabled": True}],
        "credentials": [],
        "models": [],
        "provider_models": [],
    }


def make_db_row(version: int, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": version,
        "schema_version": 1,
        "payload": json.dumps(payload),
        "checksum": configuration_checksum(payload),
        "published_at": datetime(2026, 9, 1, tzinfo=UTC),
    }


def make_header_row(version: int, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": version,
        "checksum": configuration_checksum(payload),
    }


@pytest.mark.asyncio
async def test_initial_startup_fetches_lightweight_header_then_full_payload() -> None:
    payload1 = make_payload("v1")
    header = make_header_row(1, payload1)
    full_row = make_db_row(1, payload1)

    pool = FakePool([header, full_row])
    repo = PostgresSnapshotRepository(pool)

    snapshot = await repo.load_published()

    assert snapshot is not None
    assert snapshot.version == 1
    assert snapshot.payload["name"] == "v1"
    assert snapshot.verify_checksum()

    # Verify exactly two queries were executed:
    # 1. Lightweight header query (id, checksum)
    # 2. Full row query (payload)
    assert len(pool.queries) == 2
    assert "select id, checksum" in pool.queries[0][0]
    assert "select id, schema_version, payload" in pool.queries[1][0]
    assert pool.queries[1][1] == (1,)


@pytest.mark.asyncio
async def test_unchanged_config_does_not_trigger_full_payload_query() -> None:
    payload1 = make_payload("v1")
    header = make_header_row(1, payload1)
    full_row = make_db_row(1, payload1)

    # Sequence:
    # Call 1 (startup): header -> full row
    # Call 2 (poll 1): header (unchanged) -> NO full row
    # Call 3 (poll 2): header (unchanged) -> NO full row
    pool = FakePool([header, full_row, header, header])
    repo = PostgresSnapshotRepository(pool)

    # Initial load
    first = await repo.load_published()
    assert first is not None
    assert len(pool.queries) == 2

    # Second load (poll 1) - unchanged version and checksum
    second = await repo.load_published()
    assert second is first  # exact cached snapshot reused
    assert len(pool.queries) == 3
    # Only header query executed!
    assert "select id, checksum" in pool.queries[2][0]
    assert "payload" not in pool.queries[2][0]

    # Third load (poll 2) - unchanged version and checksum
    third = await repo.load_published()
    assert third is first
    assert len(pool.queries) == 4
    # Only header query executed!
    assert "select id, checksum" in pool.queries[3][0]
    assert "payload" not in pool.queries[3][0]


@pytest.mark.asyncio
async def test_changed_version_triggers_exactly_one_full_payload_fetch() -> None:
    payload1 = make_payload("v1")
    payload2 = make_payload("v2")

    header1 = make_header_row(1, payload1)
    full_row1 = make_db_row(1, payload1)

    header2 = make_header_row(2, payload2)
    full_row2 = make_db_row(2, payload2)

    # Sequence:
    # 1. Call 1: header1 -> full_row1
    # 2. Call 2: header1 (no-op)
    # 3. Call 3: header2 -> full_row2
    # 4. Call 4: header2 (no-op)
    pool = FakePool([header1, full_row1, header1, header2, full_row2, header2])
    repo = PostgresSnapshotRepository(pool)

    s1 = await repo.load_published()
    assert s1 is not None and s1.version == 1

    s1_cached = await repo.load_published()
    assert s1_cached is s1

    # New version published
    s2 = await repo.load_published()
    assert s2 is not None and s2.version == 2
    assert s2.payload["name"] == "v2"

    s2_cached = await repo.load_published()
    assert s2_cached is s2

    # Query breakdown:
    # 1: header1
    # 2: full_row1
    # 3: header1
    # 4: header2
    # 5: full_row2
    # 6: header2
    assert len(pool.queries) == 6
    payload_queries = [q for q, _ in pool.queries if "payload" in q]
    assert len(payload_queries) == 2  # Exactly 2 full payload queries in total!


@pytest.mark.asyncio
async def test_changed_checksum_same_version_triggers_full_payload_fetch() -> None:
    payload1 = make_payload("v1")
    payload2 = make_payload("v1_modified")

    header1 = make_header_row(1, payload1)
    full_row1 = make_db_row(1, payload1)

    header2 = make_header_row(1, payload2)  # Same id=1, but different checksum
    full_row2 = make_db_row(1, payload2)

    pool = FakePool([header1, full_row1, header2, full_row2])
    repo = PostgresSnapshotRepository(pool)

    s1 = await repo.load_published()
    assert s1 is not None and s1.payload["name"] == "v1"

    # Checksum mismatch forces payload reload even if version id is unchanged
    s2 = await repo.load_published()
    assert s2 is not None and s2.payload["name"] == "v1_modified"
    assert len(pool.queries) == 4


@pytest.mark.asyncio
async def test_no_published_version_returns_none() -> None:
    pool = FakePool([None])
    repo = PostgresSnapshotRepository(pool)

    snapshot = await repo.load_published()
    assert snapshot is None
    assert len(pool.queries) == 1
    assert "select id, checksum" in pool.queries[0][0]


@pytest.mark.asyncio
async def test_cached_configuration_integration_skips_payload_transfer() -> None:
    payload1 = make_payload("v1")
    header1 = make_header_row(1, payload1)
    full_row1 = make_db_row(1, payload1)

    pool = FakePool([header1, full_row1, header1, header1])
    repo = PostgresSnapshotRepository(pool)
    cache = CachedConfiguration(repo)

    # Initial startup refresh
    snap1 = await cache.refresh()
    assert snap1.version == 1

    # Polling refresh 1: should not download payload
    snap2 = await cache.refresh()
    assert snap2 is snap1

    # Polling refresh 2: should not download payload
    snap3 = await cache.refresh()
    assert snap3 is snap1

    # 4 queries total: 1 header + 1 full + 2 headers only
    assert len(pool.queries) == 4
    payload_queries = [q for q, _ in pool.queries if "payload" in q]
    assert len(payload_queries) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("host,port,cache_size", [
    ("aws-1-eu-west-1.pooler.supabase.com", 6543, 0),
    ("aws-1-eu-west-1.pooler.supabase.com", 5432, 100),
    ("localhost", 6543, 100),
])
async def test_pool_disables_statement_cache_for_supavisor_transaction_mode(
    monkeypatch, host, port, cache_size,
):
    factory = AsyncMock()
    monkeypatch.setattr("gateway.configuration.postgres.asyncpg.create_pool", factory)
    dsn = f"postgresql://postgres:synthetic@{host}:{port}/postgres"
    pool = await create_pool(dsn)
    assert pool.raw_pool is factory.return_value
    assert factory.call_args.args == (dsn,)
    assert factory.call_args.kwargs["statement_cache_size"] == cache_size


@pytest.mark.asyncio
@pytest.mark.parametrize("dsn", [
    "postgresql://postgres:synthetic@aws-1-eu-west-1.pooler.supabase.com/postgres?port=6543",
    "postgresql://postgres:synthetic@localhost/postgres"
    "?host=aws-1-eu-west-1.pooler.supabase.com&port=6543",
])
async def test_pool_detects_transaction_endpoint_query_overrides(monkeypatch, dsn):
    factory = AsyncMock()
    monkeypatch.setattr("gateway.configuration.postgres.asyncpg.create_pool", factory)
    await create_pool(dsn)
    assert factory.call_args.kwargs["statement_cache_size"] == 0
