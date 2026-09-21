"""Issue a gateway key for the synthetic qualification client (dev only)."""

from __future__ import annotations

import os
import sys
from uuid import uuid4
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gateway.config import get_settings  # noqa: E402
from gateway.security.gateway_keys import GatewayKeyHasher  # noqa: E402

CLIENT_ID = "10000000-0000-0000-0000-000000000001"


def _dsn() -> str:
    env = Path(__file__).resolve().parents[1] / ".env.dbtest"
    load_dotenv(env)
    dsn = os.environ["POOLER"]
    return dsn


async def main() -> None:
    settings = get_settings()
    hasher = GatewayKeyHasher.from_base64(settings.key_pepper)
    dsn = _dsn()
    connection = await asyncpg.connect(dsn, statement_cache_size=0)
    try:
        key_id = str(uuid4())
        issued = hasher.issue(key_id=key_id, client_id=CLIENT_ID)
        await connection.execute(
            """
            insert into public.gateway_client_keys(
              id,client_id,key_prefix,key_digest,label)
            values($1,$2,$3,$4,'atria probe')
            on conflict(id) do nothing
            """,
            key_id,
            CLIENT_ID,
            issued.record.key_prefix,
            issued.record.digest,
        )
        print(f"client key issued: {issued.plaintext}")
    finally:
        await connection.close()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
