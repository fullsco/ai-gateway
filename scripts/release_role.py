"""Installed-wheel role entrypoint; imports gateway only from the selected environment."""

import argparse
import asyncio
import json
import signal
import sys
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from release_support import (  # noqa: E402
    PORTS,
    ReleaseIdentity,
    provenance,
    provider_app,
    release_dsn,
)
from staging_boundary import disable_dotenv, require_staging  # noqa: E402


def runtime(config):
    import httpx

    from gateway.auth import ClientPermissions, GatewayClient, InMemoryGatewayKeyStore
    from gateway.models import CanonicalModel, ModelRegistry, ProviderModel
    from gateway.protocols import Capability, ClientProtocol
    from gateway.providers import Credential, ProviderConfig
    from gateway.providers.openai import OpenAICompatibleAdapter
    from gateway.routing import CredentialState, ProviderState, RoutingEngine
    from gateway.runtime import GatewayRuntime
    from gateway.security.gateway_keys import GatewayKey, GatewayKeyHasher

    protocol = ClientProtocol.OPENAI_CHAT_COMPLETIONS
    caps = frozenset({Capability.STREAMING, Capability.TOOL_CALLING})
    mapping = ProviderModel(config["mapping"], "qualification-model", config["provider"],
        "synthetic", protocol, caps, pricing={"per_request": "0.01", "currency": "USD"})
    registry = ModelRegistry([CanonicalModel("qualification-model", frozenset(), caps)], [mapping])
    adapter = OpenAICompatibleAdapter(ProviderConfig(
        id=config["provider"], name="release-synthetic",
        base_url=f"http://127.0.0.1:{PORTS['provider']}", protocol=protocol, capabilities=caps,
        timeout_seconds=30))
    key = GatewayKey(id=config["key_id"], client_id=config["client"],
                     key_prefix=config["prefix"], digest=config["digest"])
    return GatewayRuntime(key_store=InMemoryGatewayKeyStore([key], [GatewayClient(config["client"],
        "release-synthetic", ClientPermissions(frozenset({protocol})))]),
        key_hasher=GatewayKeyHasher(bytes.fromhex(config["pepper"])), model_registry=registry,
        routing_engine=RoutingEngine(registry),
        provider_states=(ProviderState(config["provider"]),),
        credential_states=(CredentialState(config["credential"], config["provider"]),),
        provider_model_adapters={config["mapping"]: adapter},
        credentials={config["credential"]: Credential(id=config["credential"], secret="synthetic")},
        http_client=httpx.AsyncClient(trust_env=False, timeout=30))


async def run(role, config, manifest, evidence):
    import asyncpg
    import uvicorn

    label = "baseline" if role == "baseline" else "current"
    identity = provenance(manifest, label)
    if role == "provider":
        await uvicorn.Server(uvicorn.Config(provider_app(), host="127.0.0.1",
            port=PORTS["provider"], access_log=False, log_level="error")).serve()
        return
    from gateway.app import create_app
    from gateway.config import Settings

    db = await asyncpg.create_pool(release_dsn(config["dsn"]), min_size=1, max_size=3,
        command_timeout=10, timeout=5, statement_cache_size=0)
    rt = runtime(config)
    options = {"environment": "test", "database_url": None, "health_probe_enabled": False,
        "alert_monitor_enabled": False, "alert_delivery_enabled": False,
        "credential_usage_poll_enabled": False, "live_state_refresh_seconds": 300,
        "log_level": "ERROR", "failover_enabled": False, "stream_keepalive_seconds": .5}
    if label == "current":
        options.update(service_role="api" if role in {"current", "jobs"} else role,
            workflows_enabled=True, durable_accounting_enabled=True,
            distributed_admission_enabled=True, shutdown_timeout_seconds=20)
    app = create_app(Settings(_env_file=None, **options), rt, db_pool=db)

    @app.get("/qualification/provenance")
    async def identify():
        return identity

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def recorded_lifespan(application):
        clean = False
        try:
            async with original_lifespan(application):
                yield
            clean = not application.state.ready
        finally:
            await rt.http_client.aclose()
            await asyncio.wait_for(db.close(), 10)
            # Uvicorn re-raises SIGTERM after lifespan shutdown; persist before serve returns.
            evidence.write_text(json.dumps({"role": role, **identity, "lifespan_closed": clean,
                                            "pool_closed": db.is_closing()}))

    app.router.lifespan_context = recorded_lifespan
    try:
        if role in {"baseline", "current", "jobs"}:
            await uvicorn.Server(uvicorn.Config(
                ReleaseIdentity(app, label, jobs_only=role == "jobs"), host="127.0.0.1",
                port=PORTS[role], access_log=False, log_level="error",
                timeout_graceful_shutdown=20)).serve()
        else:
            from gateway.worker_service import workflow_loop

            stop = asyncio.Event()
            for sig in (signal.SIGINT, signal.SIGTERM):
                asyncio.get_running_loop().add_signal_handler(sig, stop.set)
            async with app.router.lifespan_context(app):
                task = asyncio.create_task(workflow_loop(app)) if role == "workflow" else None
                try:
                    await stop.wait()
                finally:
                    await app.state.drain.drain(20)
                    if task is not None:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
    finally:
        await rt.http_client.aclose()
        await asyncio.wait_for(db.close(), 10)


if __name__ == "__main__":
    require_staging()
    disable_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=["baseline", "current", "jobs", "workflow",
                                        "maintenance", "provider"])
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(run(args.role, json.loads(args.config.read_text()),
                    json.loads(args.manifest.read_text()), args.evidence))
