import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("mode", ["--module", "--script"])
def test_offline_child_blocks_dotenv_database_and_external_sockets(tmp_path, mode):
    # The child checks behavior without ever opening dotenv or attempting a socket.
    source = '''
import asyncio
import json
import socket
import asyncpg
from gateway.config import Settings

assert Settings.model_config["env_file"] is None
settings = Settings()
assert not settings.database_url
assert not settings.supabase_url
assert settings.environment == "test"

async def check():
    for connect in (asyncpg.connect, asyncpg.connection.connect):
        try:
            await connect("postgresql://127.0.0.1:1/offline")
        except RuntimeError as exc:
            assert "forbid database" in str(exc)
        else:
            raise AssertionError("database guard missing")
    try:
        async with asyncpg.create_pool(
            "postgresql://127.0.0.1:1/offline", min_size=1, max_size=1
        ):
            raise AssertionError("pool guard missing")
    except RuntimeError as exc:
        assert "forbid database" in str(exc)

asyncio.run(check())
for method in ("connect", "connect_ex"):
    with socket.socket() as sock:
        try:
            getattr(sock, method)(("192.0.2.1", 1))
        except RuntimeError as exc:
            assert "forbid external" in str(exc)
        else:
            raise AssertionError("socket guard missing")
print(json.dumps({"guards": "passed"}))
'''
    script = tmp_path / "offline_probe.py"
    script.write_text(source)
    target = ["offline_probe"] if mode == "--module" else [str(script)]
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/dev_check.py"), mode, *target],
        cwd=ROOT, capture_output=True, text=True, timeout=15,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"guards": "passed"}
