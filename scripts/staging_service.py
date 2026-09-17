"""Synthetic deployed API/worker/provider roles restricted to the authorized AWS staging host."""

import argparse
import asyncio
import json
import os
import signal
import socket
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if socket.gethostname() != "ip-172-31-42-222":
    raise SystemExit("Staging services are restricted to the authorized AWS host")

for name in list(os.environ):
    if name.startswith(("GATEWAY_", "SUPABASE_")) or name in {
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    }:
        os.environ.pop(name)

from gateway.config import Settings, get_settings  # noqa: E402

Settings.model_config["env_file"] = None
get_settings.cache_clear()
QUERY_PROFILE = Counter()


def query_observed(record):
    # Driver wall time includes scheduling/transport; never persist SQL or arguments.
    from scripts.qualification_tracing import driver_query_observed, query_operation

    operation = query_operation(record.query)
    QUERY_PROFILE[operation + "_calls"] += 1
    QUERY_PROFILE[operation + "_seconds"] += record.elapsed
    QUERY_PROFILE["errors"] += int(record.exception is not None)
    driver_query_observed(record)


def settings(role):
    return Settings(_env_file=None, database_url=None, environment="test", service_role=role,
        health_probe_enabled=False, alert_monitor_enabled=False,
        credential_usage_poll_enabled=False,
        workflows_enabled=True, durable_accounting_enabled=True, distributed_admission_enabled=True,
        max_in_flight_requests=16, max_in_flight_per_client=8, max_in_flight_streams=12,
        distributed_provider_capacity=8, distributed_credential_capacity=8,
        workflow_global_capacity=2, workflow_per_client_capacity=1,
        config_refresh_seconds=3600, live_state_refresh_seconds=30,
        shutdown_timeout_seconds=20, stream_keepalive_seconds=.5, log_level="WARNING")


async def make_runtime():
    import httpx

    from gateway.auth import ClientPermissions, GatewayClient, InMemoryGatewayKeyStore
    from gateway.models import CanonicalModel, ModelRegistry, ProviderModel
    from gateway.protocols import Capability, ClientProtocol
    from gateway.providers import Credential, ProviderConfig
    from gateway.providers.openai import OpenAICompatibleAdapter
    from gateway.routing import CredentialState, ProviderState, RouteControls, RoutingEngine
    from gateway.runtime import GatewayRuntime
    from gateway.security.gateway_keys import GatewayKey, GatewayKeyHasher
    from scripts.seed_dbtest import CREDENTIAL, MAPPING, PROVIDER

    config = json.loads((ROOT / ".staging-config").read_text())
    protocol = ClientProtocol.OPENAI_CHAT_COMPLETIONS
    caps = frozenset({Capability.STREAMING})
    clients = [GatewayClient(item["id"], name, ClientPermissions(frozenset({protocol})))
               for name, item in config["clients"].items()]
    keys = [GatewayKey(id=item["key_id"], client_id=item["id"], key_prefix=item["prefix"],
                       digest=item["digest"]) for item in config["clients"].values()]
    model = CanonicalModel("qualification-model", frozenset(), caps)
    mapping = ProviderModel(str(MAPPING), model.id, str(PROVIDER), "synthetic", protocol, caps,
                            max_concurrency=8, pricing={"per_request": "0.01", "currency": "USD"})
    registry = ModelRegistry([model], [mapping])
    adapter = OpenAICompatibleAdapter(ProviderConfig(id=str(PROVIDER), name="synthetic",
        base_url="http://127.0.0.1:18340", protocol=protocol, capabilities=caps,
        timeout_seconds=90))
    return GatewayRuntime(
        key_store=InMemoryGatewayKeyStore(keys, clients), key_hasher=GatewayKeyHasher(b"s" * 32),
        model_registry=registry, routing_engine=RoutingEngine(registry),
        provider_states=(ProviderState(str(PROVIDER)),),
        credential_states=(CredentialState(str(CREDENTIAL), str(PROVIDER)),),
        provider_model_adapters={str(MAPPING): adapter},
        credentials={str(CREDENTIAL): Credential(id=str(CREDENTIAL), secret="synthetic")},
        http_client=httpx.AsyncClient(trust_env=False, limits=httpx.Limits(max_connections=16)),
        route_controls=RouteControls({str(MAPPING): 8}),
    )


async def database():
    import asyncpg

    from gateway.configuration.measured_pool import MeasuredPool
    from scripts.qualification_tracing import TracedConnection

    async def initialize(connection):
        connection.add_query_logger(query_observed)

    dsn = (ROOT / ".staging-dsn").read_text().strip()
    if not args.direct_db:
        dsn = dsn.replace(":5432/", ":18432/")
    return MeasuredPool(await asyncpg.create_pool(
        dsn, min_size=1, max_size=5, command_timeout=10, timeout=5,
        statement_cache_size=100, server_settings={"application_name": "ai-gateway"},
        init=initialize,
        connection_class=TracedConnection if args.trace_queries else asyncpg.Connection,
    ))


async def api(port):
    import uvicorn

    from gateway.app import create_app

    pool, runtime = await database(), await make_runtime()

    class Verifier:
        async def verify(self, token):
            from gateway.admin.auth import AdminClaims

            config = json.loads((ROOT / ".staging-config").read_text())
            if token != config["admin_token"]:
                raise ValueError("Invalid synthetic administrator")
            return AdminClaims("synthetic", None, "admin")

    app = create_app(settings("api"), runtime, db_pool=pool, admin_verifier=Verifier())
    if args.trace_queries:
        from scripts.qualification_tracing import (
            RequestTraceCollector,
            RequestTraceMiddleware,
            archive_on_shutdown,
        )

        collector = RequestTraceCollector()
        app.add_middleware(RequestTraceMiddleware, collector=collector)
        archive_on_shutdown(app, collector, ROOT / "evidence" / f"request-traces-api-{port}.json")
    from fastapi import Request
    from fastapi.responses import JSONResponse

    @app.get("/qualification/query-profile")
    async def query_profile(request: Request):
        config = json.loads((ROOT / ".staging-config").read_text())
        if request.headers.get("authorization") != "Bearer " + config["admin_token"]:
            return JSONResponse({}, status_code=401)
        return dict(QUERY_PROFILE)

    try:
        await uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
            access_log=False, log_level="warning", timeout_graceful_shutdown=20)).serve()
    finally:
        await runtime.http_client.aclose()
        await pool.close()
        (ROOT / "evidence" / f"queries-api-{port}.json").write_text(json.dumps(QUERY_PROFILE))


async def worker(role):
    from gateway.app import create_app
    from gateway.worker_service import workflow_loop

    pool, runtime = await database(), await make_runtime()
    app = create_app(settings(role), runtime, db_pool=pool)
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    try:
        async with app.router.lifespan_context(app):
            task = asyncio.create_task(workflow_loop(app)) if role == "workflow" else None
            waiter = asyncio.create_task(stop.wait())
            try:
                if task:
                    done, _ = await asyncio.wait({task, waiter},
                                                 return_when=asyncio.FIRST_COMPLETED)
                    if task in done:
                        await task
                else:
                    await waiter
            finally:
                await app.state.drain.drain(20)
                waiter.cancel()
                if task:
                    task.cancel()
                await asyncio.gather(*(item for item in (task, waiter) if item),
                                     return_exceptions=True)
    finally:
        await runtime.http_client.aclose()
        await pool.close()


async def provider():
    import uvicorn
    from fastapi import FastAPI, Request
    from fastapi.responses import StreamingResponse

    app = FastAPI()
    state = {"requests": 0, "active": 0, "peak": 0}

    @app.get("/health")
    async def health():
        return state

    @app.post("/v1/chat/completions")
    async def completion(request: Request):
        body = await request.json()
        state["requests"] += 1
        state["active"] += 1
        state["peak"] = max(state["peak"], state["active"])
        delay = min(45, max(.01, float(body.get("metadata", {}).get("delay", .05))))
        if body.get("stream"):
            # For HTTP ASGI spec >=2.4, Starlette relies on send errors instead of
            # listening for disconnects. A sleeping synthetic generator needs the
            # disconnect listener to model upstream cancellation promptly.
            request.scope["asgi"]["spec_version"] = "2.3"
            async def events():
                try:
                    yield b'data: {"choices":[{"index":0,"delta":{"content":"synthetic"}}]}\n\n'
                    deadline = asyncio.get_running_loop().time() + delay
                    while asyncio.get_running_loop().time() < deadline:
                        if await request.is_disconnected():
                            return
                        await asyncio.sleep(min(.1, delay))
                        yield b": synthetic-upstream-heartbeat\n\n"
                    yield (b'data: {"choices":[],"usage":{"prompt_tokens":4,'
                           b'"completion_tokens":1}}\n\n')
                    yield b'data: [DONE]\n\n'
                finally:
                    state["active"] -= 1
            return StreamingResponse(events(), media_type="text/event-stream")
        try:
            await asyncio.sleep(delay)
            return {"id": "synthetic", "object": "chat.completion", "choices": [
                {"index": 0, "message": {"role": "assistant", "content": "ok"},
                 "finish_reason": "stop"}
            ], "usage": {"prompt_tokens": 4, "completion_tokens": 1}}
        finally:
            state["active"] -= 1

    await uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=18340,
                                       access_log=False, log_level="warning")).serve()


async def proxy():
    async def connection(reader, writer):
        upstream = None
        try:
            remote, upstream = await asyncio.open_connection("127.0.0.1", 5432)

            async def relay(source, target):
                while data := await source.read(65536):
                    target.write(data)
                    await target.drain()

            tasks = [asyncio.create_task(relay(reader, upstream)),
                     asyncio.create_task(relay(remote, writer))]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            writer.close()
            if upstream:
                upstream.close()

    server = await asyncio.start_server(connection, "127.0.0.1", 18432)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=["api", "workflow", "maintenance", "provider", "proxy"])
    parser.add_argument("--port", type=int, default=18330, choices=[18330, 18331])
    parser.add_argument("--direct-db", action="store_true")
    parser.add_argument("--trace-queries", action="store_true")
    args = parser.parse_args()
    functions = {"api": lambda: api(args.port), "workflow": lambda: worker("workflow"),
                 "maintenance": lambda: worker("maintenance"), "provider": provider, "proxy": proxy}
    try:
        asyncio.run(functions[args.role]())
    finally:
        name = f"api-{args.port}" if args.role == "api" else args.role
        (ROOT / "evidence" / f"queries-{name}.json").write_text(json.dumps(QUERY_PROFILE))
