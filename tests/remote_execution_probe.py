"""Finite real-executor workflow crash probe; provider transport is synthetic and offline."""

import asyncio
import json
import os
import sys

from scripts.dev_db_check import configure_environment, install_database_bounds, validate_target


async def run(client_id, phase):
    import httpx

    from gateway.accounting import FinancialLedger
    from gateway.auth import ClientPermissions, GatewayClient, InMemoryGatewayKeyStore
    from gateway.config import Settings
    from gateway.configuration import create_pool
    from gateway.models import CanonicalModel, ModelRegistry, ProviderModel
    from gateway.protocols import ClientProtocol
    from gateway.providers import Credential, ProviderConfig
    from gateway.providers.openai import OpenAICompatibleAdapter
    from gateway.routing import CredentialState, ProviderState, RoutingEngine
    from gateway.runtime import GatewayRuntime
    from gateway.security.gateway_keys import GatewayKeyHasher
    from gateway.workflow_worker import WorkflowWorker
    from scripts.seed_dbtest import CREDENTIAL, MAPPING, PROVIDER

    async def handler(request):
        assert request.url.host == "127.0.0.1"
        if phase == "accepted":
            print(json.dumps({"phase": "accepted"}), flush=True)
            await asyncio.Event().wait()
        return httpx.Response(200, json={"id": "synthetic", "object": "chat.completion",
            "choices": [{"message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 1}})

    if phase == "usage":
        original = FinancialLedger.settle

        async def stop_after_usage(self, reservation_id, *, actual):
            if actual is not None:
                print(json.dumps({"phase": "usage", "reservation_id": str(reservation_id)}),
                      flush=True)
                await asyncio.Event().wait()
            await original(self, reservation_id, actual=actual)

        FinancialLedger.settle = stop_after_usage
    protocol = ClientProtocol.OPENAI_CHAT_COMPLETIONS
    registry = ModelRegistry([CanonicalModel("qualification-model", frozenset(), frozenset())], [
        ProviderModel(str(MAPPING), "qualification-model", str(PROVIDER), "synthetic", protocol,
                      frozenset(), pricing={"per_request": "0.25", "currency": "USD"}),
    ])
    adapter = OpenAICompatibleAdapter(ProviderConfig(id=str(PROVIDER), name="synthetic",
        base_url="http://127.0.0.1", protocol=protocol, capabilities=frozenset()))
    runtime = GatewayRuntime(
        key_store=InMemoryGatewayKeyStore([], [GatewayClient(client_id, "synthetic",
            ClientPermissions(frozenset({protocol})))]), key_hasher=GatewayKeyHasher(b"x" * 32),
        model_registry=registry, routing_engine=RoutingEngine(registry),
        provider_states=(ProviderState(str(PROVIDER)),),
        credential_states=(CredentialState(str(CREDENTIAL), str(PROVIDER)),),
        provider_model_adapters={str(MAPPING): adapter},
        credentials={str(CREDENTIAL): Credential(id=str(CREDENTIAL), secret="synthetic")},
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False),
    )
    pool = await create_pool(os.environ["GATEWAY_DATABASE_URL"])
    try:
        async with asyncio.timeout(90):
            await WorkflowWorker(pool, runtime, Settings(_env_file=None,
                durable_accounting_enabled=True, health_probe_enabled=False)).run_once()
    finally:
        await runtime.http_client.aclose()
        await pool.close()


if __name__ == "__main__":
    dsn = os.environ["GATEWAY_DATABASE_URL"]
    validate_target(dsn, "kpmxgwflgyzjvitwlfvs")
    configure_environment(dsn)
    from gateway.config import Settings

    Settings.model_config["env_file"] = None
    install_database_bounds(dsn)
    if sys.argv[2] not in {"accepted", "usage"}:
        raise SystemExit("Unsupported synthetic crash phase")
    asyncio.run(run(sys.argv[1], sys.argv[2]))
