"""Run only on the explicitly authorized AWS staging host, never in the coding environment."""

import asyncio
import json
import os
import secrets
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
HOST = "ip-172-31-42-222"
DATABASE = "gateway_qualification"


def provision():
    if socket.gethostname() != HOST:
        raise SystemExit("This setup is restricted to the authorized AWS staging host")
    destination = ROOT / ".staging-dsn"
    if destination.exists():
        return
    password = secrets.token_hex(24)
    sql = (
        f"CREATE ROLE {DATABASE} LOGIN PASSWORD '{password}';\n"
        f"CREATE DATABASE {DATABASE} OWNER {DATABASE};\n"
    )
    subprocess.run(["sudo", "-n", "-u", "postgres", "psql", "-X", "-v", "ON_ERROR_STOP=1"],
                   input=sql, text=True, check=True, stdout=subprocess.DEVNULL)
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(f"postgresql://{DATABASE}:{password}@127.0.0.1:5432/{DATABASE}")


async def migrate():
    import asyncpg

    from scripts.prepare_dbtest import prepare

    dsn = (ROOT / ".staging-dsn").read_text().strip()
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("create schema if not exists auth")
        await conn.execute("create table if not exists auth.users(id uuid primary key)")
    finally:
        await conn.close()
    result = await prepare(dsn)
    result["project_ref"] = "aws-staging-gateway_qualification"
    print(json.dumps(result))


if __name__ == "__main__":
    provision()
    asyncio.run(migrate())
