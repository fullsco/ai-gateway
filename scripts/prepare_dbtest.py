"""Prepare only the dedicated remote qualification project with migrations and synthetic rows."""

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.dev_db_check import load_database_url, validate_target  # noqa: E402

PROJECT = "kpmxgwflgyzjvitwlfvs"


def migration_body(source):
    # These repository files have only standalone outer transaction delimiters.
    # Remove those so migration + journal insertion can commit atomically.
    return "\n".join(line for line in source.splitlines()
                     if line.strip().lower() not in {"begin;", "commit;"})


async def prepare(dsn):
    import asyncpg

    connection = await asyncpg.connect(dsn, timeout=10, command_timeout=20, statement_cache_size=0)
    try:
        journal = await connection.fetchval("select to_regclass('public.qualification_migrations')")
        if journal is None:
            count = await connection.fetchval(
                "select count(*) from pg_tables where schemaname='public'"
            )
            if count:
                raise RuntimeError("Dedicated target is not empty and has no qualification journal")
            await connection.execute(
                "create table public.qualification_migrations "
                "(name text primary key, checksum text not null, "
                "applied_at timestamptz default now());"
                "alter table public.qualification_migrations enable row level security;"
                "revoke all on public.qualification_migrations from public,anon,authenticated"
            )
        applied = []
        for path in sorted((ROOT / "migrations").glob("*.sql")):
            source = path.read_text()
            checksum = hashlib.sha256(source.encode()).hexdigest()
            existing = await connection.fetchval(
                "select checksum from public.qualification_migrations where name=$1", path.name,
            )
            if existing:
                if existing != checksum:
                    raise RuntimeError(f"Applied migration changed: {path.name}")
                continue
            async with connection.transaction():
                await connection.execute("set local lock_timeout='3s'")
                await connection.execute("set local statement_timeout='20s'")
                if path.name.startswith("009_"):
                    # Historical data migration requires two legacy records. Both are synthetic,
                    # use loopback URLs, and are removed immediately after testing consolidation.
                    for name, protocol, provider_type in (
                        ("AgentRouter Anthropic", "anthropic_messages", "anthropic_compatible"),
                        ("AgentRouter OpenAI", "openai_chat_completions", "openai_compatible"),
                    ):
                        await connection.execute(
                            "insert into public.providers(name,protocol,provider_type,base_url) "
                            "values($1,$2,$3,'http://127.0.0.1')", name, protocol, provider_type,
                        )
                await connection.execute(migration_body(source))
                if path.name.startswith("009_"):
                    await connection.execute(
                        "delete from public.providers where name=any($1::text[])",
                        ["AgentRouter", "AgentRouter OpenAI (archived)"],
                    )
                await connection.execute(
                    "insert into public.qualification_migrations(name,checksum) values($1,$2)",
                    path.name, checksum,
                )
            applied.append(path.name)
        from scripts.seed_dbtest import seed

        await seed(connection)
        return {"project_ref": PROJECT, "applied": applied, "synthetic_seed": "ready"}
    finally:
        await connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env.dbtest")
    args = parser.parse_args()
    dsn = load_database_url(args.env_file)
    validate_target(dsn, PROJECT)
    # Only the explicitly dedicated test project can be initialized by this script.
    result = asyncio.run(prepare(dsn))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
