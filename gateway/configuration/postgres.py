import json
from typing import Any
from urllib.parse import parse_qs, urlsplit

import asyncpg

from gateway.configuration.measured_pool import MeasuredPool
from gateway.configuration.snapshots import ConfigSnapshot


class PostgresSnapshotRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool
        self._cached_snapshot: ConfigSnapshot | None = None

    async def load_published(
        self,
        cached: ConfigSnapshot | None = None,
    ) -> ConfigSnapshot | None:
        """Load published configuration snapshot conditionally.

        Fetches only lightweight metadata (id, checksum) first. If the published
        version and checksum match the currently cached snapshot, the full payload
        (~67 KB) is not downloaded. The full payload is only queried when no snapshot
        is cached or when the version/checksum has changed.
        """
        if cached is not None:
            self._cached_snapshot = cached

        header = await self._pool.fetchrow(
            """
            select id, checksum
            from public.config_versions
            where status = 'published'
            limit 1
            """
        )
        if header is None:
            return None

        if (
            self._cached_snapshot is not None
            and self._cached_snapshot.version == header["id"]
            and self._cached_snapshot.checksum == header["checksum"]
        ):
            return self._cached_snapshot

        row = await self._pool.fetchrow(
            """
            select id, schema_version, payload, checksum, published_at
            from public.config_versions
            where id = $1
            """,
            header["id"],
        )
        if row is None:
            row = await self._pool.fetchrow(
                """
                select id, schema_version, payload, checksum, published_at
                from public.config_versions
                where status = 'published'
                limit 1
                """
            )
            if row is None:
                return None

        payload: Any = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        snapshot = ConfigSnapshot(
            version=row["id"],
            schema_version=row["schema_version"],
            payload=payload,
            checksum=row["checksum"],
            published_at=row["published_at"],
        )
        self._cached_snapshot = snapshot
        return snapshot


async def create_pool(database_url: str) -> MeasuredPool:
    target = urlsplit(database_url)
    query = parse_qs(target.query)
    hosts = query.get("host", [target.hostname or ""])[-1].split(",")
    ports = query.get("port", [str(target.port or 5432)])[-1].split(",")
    transaction_pooler = (
        "6543" in ports
        and any(host.endswith(".pooler.supabase.com") for host in hosts)
    )
    pool = await asyncpg.create_pool(
        database_url,
        min_size=1,
        max_size=5,
        command_timeout=10,
        timeout=5,
        # Supavisor transaction mode cannot retain prepared statements across transactions.
        statement_cache_size=0 if transaction_pooler else 100,
        server_settings={"application_name": "ai-gateway"},
    )
    return MeasuredPool(pool)
