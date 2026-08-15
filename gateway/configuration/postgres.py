import json
from typing import Any

import asyncpg

from gateway.configuration.snapshots import ConfigSnapshot


class PostgresSnapshotRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def load_published(self) -> ConfigSnapshot | None:
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
        return ConfigSnapshot(
            version=row["id"],
            schema_version=row["schema_version"],
            payload=payload,
            checksum=row["checksum"],
            published_at=row["published_at"],
        )


async def create_pool(database_url: str) -> asyncpg.Pool:
    return await asyncpg.create_pool(
        database_url,
        min_size=1,
        max_size=5,
        command_timeout=10,
        server_settings={"application_name": "ai-gateway"},
    )
