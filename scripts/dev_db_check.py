"""Run PostgreSQL checks using an explicitly selected development project.

Usage: .venv/bin/python scripts/dev_db_check.py --env-file .env.dev --project-ref REF
Only the database URL is loaded; inherited gateway settings are discarded.
Uses the existing remote service, one pooled connection, and bounded connection/query waits.
Never initializes or launches a local database server.
"""

import argparse
import os
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from dotenv.parser import parse_stream

ROOT = Path(__file__).resolve().parents[1]
# Verified from ai-gateway.service on 100.62.162.5, 2026-09-13.
PRODUCTION_PROJECT_REFS = frozenset({"nebbtrmwzbpmzvinhrch"})
TEST_FILES = (
    "tests/test_phase6_integration.py",
    "tests/test_model_routing_integration.py",
    "tests/test_accounting_integration.py",
    "tests/test_workflow_integration.py",
    "tests/test_capacity_integration.py",
    "tests/test_runtime_lifecycle_remote.py",
    "tests/test_operations_remote.py",
)


def validate_target(dsn: str, project_ref: str) -> None:
    """Validate identity before connections; never include a secret DSN in errors."""
    if not re.fullmatch(r"[a-z]{20}", project_ref):
        raise ValueError("Expected a 20-letter Supabase development project reference")
    if project_ref in PRODUCTION_PROJECT_REFS:
        raise ValueError("Selected project is used by production; database qualification refused")
    try:
        target = urlsplit(dsn)
        direct = target.hostname == f"db.{project_ref}.supabase.co"
        pooled = (
            (target.hostname or "").endswith(".pooler.supabase.com")
            and target.username == f"postgres.{project_ref}"
        )
        valid = (
            target.scheme in {"postgres", "postgresql"}
            and target.netloc.count("@") == 1
            and "," not in target.netloc
            and (direct or pooled)
            and (target.port == 5432 or (pooled and target.port == 6543))
            and target.path == "/postgres"
            and not target.fragment
        )
        # Connection-string overrides could redirect a correctly named authority.
        query = parse_qs(target.query, keep_blank_values=True, strict_parsing=True)
        valid = valid and (
            not query or query in (
                {"sslmode": ["require"]},
                {"sslmode": ["verify-ca"]},
                {"sslmode": ["verify-full"]},
            )
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError(
            "Database URL does not match the selected development project/pooler port"
        )


def load_database_url(env_file: Path) -> str:
    values: dict[str, str | None] = {}
    with env_file.open() as stream:
        for binding in parse_stream(stream):
            if binding.error:
                raise ValueError("Malformed development env file")
            if binding.key not in {"GATEWAY_DATABASE_URL", "DEV_DB", "POOLER"}:
                continue
            if binding.key in values and values[binding.key] != binding.value:
                raise ValueError("Conflicting repeated database URL declarations")
            values[binding.key] = binding.value
    urls = {value for value in values.values() if value}
    if len(urls) > 1:
        raise ValueError("Conflicting database URL aliases")
    if not urls:
        raise ValueError("Development env file must contain GATEWAY_DATABASE_URL, DEV_DB or POOLER")
    return urls.pop()


def configure_environment(dsn: str) -> None:
    for key in list(os.environ):
        if key.upper().startswith(("GATEWAY_", "SUPABASE_", "PG")) or key.upper() in {
            "DATABASE_URL", "PYTEST_ADDOPTS", "PYTEST_PLUGINS",
        }:
            os.environ.pop(key)
    os.environ.update(
        GATEWAY_DATABASE_URL=dsn,
        GATEWAY_ENVIRONMENT="test",
        GATEWAY_SUPABASE_URL="",
        GATEWAY_HEALTH_PROBE_ENABLED="false",
        GATEWAY_ALERT_MONITOR_ENABLED="false",
        GATEWAY_CREDENTIAL_USAGE_POLL_ENABLED="false",
    )


def install_database_bounds(dsn: str, max_connections: int = 1):
    """Constrain this serial qualification run without changing application pool defaults."""
    import asyncpg

    if max_connections not in {1, 2}:
        raise ValueError("Qualification supports one or two connections")
    original_pool = asyncpg.create_pool
    original_connect = asyncpg.connection.connect

    def check_target(args, kwargs):
        selected = args[0] if args else kwargs.get("dsn")
        if selected != dsn or any(key in kwargs for key in ("host", "port", "user", "database")):
            raise RuntimeError("Qualification connection must use the selected development DSN")

    async def connect(*args, **kwargs):
        check_target(args, kwargs)
        kwargs["timeout"] = 10
        kwargs["command_timeout"] = 10
        return await original_connect(*args, **kwargs)

    def pool(*args, **kwargs):
        check_target(args, kwargs)
        kwargs.update(min_size=1, max_size=max_connections, timeout=10,
                      command_timeout=10, connect=connect)
        return original_pool(*args, **kwargs)

    asyncpg.connect = connect
    asyncpg.connection.connect = connect
    asyncpg.create_pool = pool


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--project-ref", required=True)
    parser.add_argument("--match", help="Run a pytest name expression within the allowed files")
    parser.add_argument("--isolated", action="store_true",
                        help="Two-connection committed-data tests; dedicated test project only")
    args = parser.parse_args()
    if not args.env_file.is_file():
        parser.error("Development env file does not exist")
    try:
        dsn = load_database_url(args.env_file)
        validate_target(dsn, args.project_ref)
        if args.isolated and args.project_ref != "kpmxgwflgyzjvitwlfvs":
            raise ValueError("Isolated qualification requires the dedicated test project")
    except ValueError as exc:
        parser.error(str(exc))
    configure_environment(dsn)
    sys.path.insert(0, str(ROOT))
    os.chdir(ROOT)

    from gateway.config import Settings, get_settings

    Settings.model_config["env_file"] = None
    get_settings.cache_clear()
    install_database_bounds(dsn, max_connections=2 if args.isolated else 1)
    import pytest

    print(f"PostgreSQL qualification target: development project {args.project_ref}")
    files = ("tests/test_remote_isolation.py", "tests/test_workflow_e2e_remote.py",
             "tests/test_artifacts_remote.py", "tests/test_shared_circuit_remote.py",
             "tests/test_qualification_tracing_remote.py", "tests/test_alert_delivery_remote.py",
             "tests/test_lane_fairness_remote.py", "tests/test_execution_crash_remote.py",
             "tests/test_mixed_workload_remote.py", "tests/test_workflow_races_remote.py",
             "tests/test_workflow_protocol_matrix_remote.py", "tests/test_rollback_remote.py") \
        if args.isolated else TEST_FILES
    selection = ["-k", args.match] if args.match else []
    return pytest.main(["-m", "integration", *files, *selection, "-x", "-ra", "--tb=short"])


if __name__ == "__main__":
    raise SystemExit(main())
