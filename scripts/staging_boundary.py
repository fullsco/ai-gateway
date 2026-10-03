"""Shared fail-closed boundaries for finite qualification on the authorized AWS host."""

import os
import socket
from pathlib import Path
from urllib.parse import urlsplit

STAGING = Path("/home/ubuntu/gateway-qualification")
HOST = "ip-172-31-42-222"


def require_staging():
    if socket.gethostname() != HOST:
        raise RuntimeError("This drill is restricted to the authorized staging host")
    return STAGING


def validate_dsn(dsn):
    try:
        target = urlsplit(dsn)
        valid = (target.scheme in {"postgres", "postgresql"}
                 and target.hostname == "127.0.0.1" and target.port == 5432
                 and target.path == "/gateway_qualification"
                 and target.username == "gateway_qualification"
                 and not target.query and not target.fragment)
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ValueError("Only the isolated loopback qualification database is permitted")
    return dsn


def staging_dsn():
    root = require_staging()
    return validate_dsn((root / ".staging-dsn").read_text().strip())


def clean_environment(home):
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home),
            "LANG": "C.UTF-8", "PYTHONNOUSERSITE": "1"}


def disable_dotenv():
    for key in list(os.environ):
        if key.startswith(("GATEWAY_", "SUPABASE_")) or key.lower() in {
            "http_proxy", "https_proxy", "all_proxy", "no_proxy",
        }:
            os.environ.pop(key)
    from gateway.config import Settings, get_settings

    Settings.model_config["env_file"] = None
    get_settings.cache_clear()


async def connect():
    import asyncpg

    return await asyncpg.connect(staging_dsn(), timeout=5, command_timeout=10,
                                 statement_cache_size=0)
