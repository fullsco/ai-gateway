import os
from unittest.mock import AsyncMock, Mock

import asyncpg
import pytest

from scripts.dev_db_check import (
    configure_environment,
    install_database_bounds,
    load_database_url,
    validate_target,
)

REF = "abcdefghijklmnopqrst"
DSN = f"postgresql://postgres.{REF}:test-secret@aws-0-test.pooler.supabase.com:5432/postgres"


@pytest.mark.parametrize("dsn", [DSN, DSN + "?sslmode=require", DSN.replace("5432", "6543"),
    f"postgresql://postgres:test-secret@db.{REF}.supabase.co:5432/postgres"])
def test_explicit_development_target_is_accepted(dsn):
    validate_target(dsn, REF)


@pytest.mark.parametrize("dsn,ref", [
    (DSN.replace(REF, "z" * 20), REF),
    (DSN.replace("5432", "6544"), REF),
    (DSN.replace(".pooler.supabase.com", ".pooler.supabase.com.attacker.invalid"), REF),
    (DSN + "?host=another-database.invalid", REF),
    (DSN + "?user=postgres.anotherproject", REF),
    (DSN + "?sslmode=disable", REF),
    (DSN + "?sslmode=require&host=another-database.invalid", REF),
    (DSN, "not-a-project"),
    (DSN.replace("5432", "invalid"), REF),
    (
        f"postgresql://postgres:synthetic@other.invalid,ignored@db.{REF}.supabase.co:5432/postgres",
        REF,
    ),
    (DSN.replace("test-secret@", "test-secret@other.invalid,ignored@"), REF),
])
def test_wrong_or_redirected_database_is_rejected_without_exposing_secrets(dsn, ref):
    with pytest.raises(ValueError) as exc:
        validate_target(dsn, ref)
    assert "test-secret" not in str(exc.value)
    assert dsn not in str(exc.value)


def test_known_production_project_is_rejected_even_when_explicitly_selected():
    production_ref = "nebbtrmwzbpmzvinhrch"
    with pytest.raises(ValueError, match="used by production"):
        validate_target(DSN.replace(REF, production_ref), production_ref)


@pytest.mark.parametrize("key", ["DEV_DB", "GATEWAY_DATABASE_URL", "POOLER"])
def test_env_file_supports_database_aliases_without_exporting_token(tmp_path, key):
    env_file = tmp_path / ".env.dev"
    env_file.write_text(f'{key}="{DSN}"\nTOKEN="synthetic-token"\n')
    assert load_database_url(env_file) == DSN


def test_env_file_rejects_ambiguous_targets(tmp_path):
    env_file = tmp_path / ".env.dev"
    env_file.write_text(f'GATEWAY_DATABASE_URL="{DSN}"\nDEV_DB="different-target"\n')
    with pytest.raises(ValueError, match="Conflicting"):
        load_database_url(env_file)


@pytest.mark.parametrize("content", [
    f'DEV_DB="different-target"\nDEV_DB="{DSN}"\n',
    f'GATEWAY_DATABASE_URL="unterminated\nDEV_DB={DSN}\n',
])
def test_env_file_rejects_duplicate_or_malformed_database_declarations(tmp_path, content):
    env_file = tmp_path / ".env.dev"
    env_file.write_text(content)
    with pytest.raises(ValueError):
        load_database_url(env_file)


def test_database_runner_replaces_inherited_settings(monkeypatch):
    # Restore the whole process environment after the runner clears settings.
    original = dict(os.environ)
    try:
        monkeypatch.setenv("GATEWAY_DATABASE_URL", "wrong-target")
        monkeypatch.setenv("GATEWAY_CREDENTIAL_ENCRYPTION_KEY", "unrelated-secret")
        monkeypatch.setenv("PGHOST", "wrong-target")
        monkeypatch.setenv("PYTEST_ADDOPTS", "--collect-only")
        configure_environment(DSN)
        assert os.environ["GATEWAY_DATABASE_URL"] == DSN
        assert os.environ["GATEWAY_ENVIRONMENT"] == "test"
        assert os.environ["GATEWAY_HEALTH_PROBE_ENABLED"] == "false"
        assert "GATEWAY_CREDENTIAL_ENCRYPTION_KEY" not in os.environ
        assert "PGHOST" not in os.environ
        assert "PYTEST_ADDOPTS" not in os.environ
    finally:
        os.environ.clear()
        os.environ.update(original)


@pytest.mark.asyncio
async def test_remote_runner_bounds_pool_and_rejects_different_connection_targets(monkeypatch):
    connect = AsyncMock()
    pool = Mock()
    monkeypatch.setattr(asyncpg, "connect", connect)
    monkeypatch.setattr(asyncpg.connection, "connect", connect)
    monkeypatch.setattr(asyncpg, "create_pool", pool)
    install_database_bounds(DSN)
    asyncpg.create_pool(DSN, min_size=1, max_size=5)
    assert pool.call_args.kwargs["max_size"] == 1
    assert pool.call_args.kwargs["timeout"] == 10
    await pool.call_args.kwargs["connect"](DSN)
    connect.assert_awaited_once_with(DSN, timeout=10, command_timeout=10)
    for call in (asyncpg.connect, asyncpg.connection.connect):
        with pytest.raises(RuntimeError, match="selected development DSN"):
            await call("postgresql://wrong-host/postgres")
        with pytest.raises(RuntimeError, match="selected development DSN"):
            await call(DSN, host="wrong-host")
    with pytest.raises(RuntimeError, match="selected development DSN"):
        asyncpg.create_pool("postgresql://wrong-host/postgres")


def test_isolated_pool_is_bounded_to_two_connections(monkeypatch):
    connect = AsyncMock()
    pool = Mock()
    monkeypatch.setattr(asyncpg, "connect", connect)
    monkeypatch.setattr(asyncpg.connection, "connect", connect)
    monkeypatch.setattr(asyncpg, "create_pool", pool)
    install_database_bounds(DSN, max_connections=2)
    asyncpg.create_pool(DSN, max_size=100)
    assert pool.call_args.kwargs["max_size"] == 2
    with pytest.raises(ValueError, match="one or two"):
        install_database_bounds(DSN, max_connections=100)
