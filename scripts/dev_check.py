"""Offline dev checks: .venv/bin/python scripts/dev_check.py [pytest arguments].

No production settings, dotenv loading, database connections, or external sockets.
Loopback remains available for the existing local subprocess qualification test.
Run database-backed integration tests separately against a disposable dev database.
"""

import ipaddress
import os
import runpy
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def is_loopback(address) -> bool:
    if not isinstance(address, tuple) or not address:
        return False
    host = address[0]
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def install_offline_guards() -> None:
    # Before importing application modules or collecting any tests.
    for name in list(os.environ):
        if name.upper().startswith(("GATEWAY_", "SUPABASE_")) or name.upper() in {
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "AI_GATEWAY",
            "PYTEST_ADDOPTS", "PYTEST_PLUGINS",
        }:
            os.environ.pop(name)
    # Subprocesses also cannot fall back to the repository's shared DB dotenv entry.
    os.environ["GATEWAY_DATABASE_URL"] = ""
    os.environ["GATEWAY_SUPABASE_URL"] = ""
    os.environ["GATEWAY_HEALTH_PROBE_ENABLED"] = "false"
    os.environ["GATEWAY_ALERT_MONITOR_ENABLED"] = "false"
    os.environ["GATEWAY_CREDENTIAL_USAGE_POLL_ENABLED"] = "false"
    os.environ["NO_PROXY"] = "*"
    sys.path.insert(0, str(ROOT))
    os.chdir(ROOT)

    import asyncpg

    from gateway.config import Settings, get_settings

    Settings.model_config["env_file"] = None
    get_settings.cache_clear()

    async def reject_database(*args, **kwargs):
        raise RuntimeError("Offline checks forbid database connections; use an isolated dev DB.")

    asyncpg.connect = reject_database
    # Pools use the connection module's entry point, not the top-level alias.
    asyncpg.connection.connect = reject_database
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def connect(sock, address):
        if not is_loopback(address):
            raise RuntimeError("Offline checks forbid external and Unix-domain connections.")
        return original_connect(sock, address)

    def connect_ex(sock, address):
        if not is_loopback(address):
            raise RuntimeError("Offline checks forbid external and Unix-domain connections.")
        return original_connect_ex(sock, address)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex


def main() -> int:
    install_offline_guards()
    # Known qualification children must use this bootstrap too: monkeypatches
    # cannot cross a process boundary. This is a dev guard, not a sandbox.
    if len(sys.argv) >= 3 and sys.argv[1] in {"--module", "--script"}:
        os.environ["GATEWAY_ENVIRONMENT"] = "test"
        mode, target = sys.argv[1:3]
        sys.argv = [target, *sys.argv[3:]]
        if mode == "--module":
            runpy.run_module(target, run_name="__main__", alter_sys=True)
        else:
            runpy.run_path(target, run_name="__main__")
        return 0

    import pytest

    print("Offline dev qualification: dotenv disabled, DB blocked, loopback sockets only.")
    return pytest.main(["-m", "not integration", *sys.argv[1:]])


if __name__ == "__main__":
    raise SystemExit(main())
