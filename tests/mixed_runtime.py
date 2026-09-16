"""Synthetic two-tenant runtime for bounded remote lane qualification."""

from uuid import uuid4

import httpx

from gateway.auth import ClientPermissions, GatewayClient, InMemoryGatewayKeyStore
from gateway.models import CanonicalModel, ModelRegistry, ProviderModel
from gateway.protocols import Capability, ClientProtocol
from gateway.providers import Credential, ProviderConfig
from gateway.providers.openai import OpenAICompatibleAdapter
from gateway.routing import CredentialState, ProviderState, RouteControls, RoutingEngine
from gateway.runtime import GatewayRuntime
from gateway.security.gateway_keys import GatewayKeyHasher
from scripts.seed_dbtest import CREDENTIAL, MAPPING, PROVIDER


async def make_mixed_runtime(pool, handler):
    hasher = GatewayKeyHasher(b"m" * 32)
    protocol = ClientProtocol.OPENAI_CHAT_COMPLETIONS
    clients, keys, issued = [], [], {}
    for lane in ("interactive", "workflow"):
        client_id, key_id = uuid4(), uuid4()
        await pool.execute(
            "insert into public.gateway_clients(id,name,spending_limit) values($1,$2,10)",
            client_id, f"Synthetic mixed {lane}",
        )
        key = hasher.issue(key_id=str(key_id), client_id=str(client_id))
        await pool.execute(
            "insert into public.gateway_client_keys(id,client_id,key_prefix,key_digest) "
            "values($1,$2,$3,$4)", key_id, client_id, key.record.key_prefix, key.record.digest,
        )
        clients.append(GatewayClient(str(client_id), lane,
                                     ClientPermissions(frozenset({protocol}))))
        keys.append(key.record)
        issued[lane] = (client_id, key.plaintext)
    capabilities = frozenset({Capability.STREAMING})
    mapping = ProviderModel(str(MAPPING), "qualification-model", str(PROVIDER), "synthetic",
        protocol, capabilities, max_concurrency=2,
        pricing={"per_request": "0.25", "currency": "USD"})
    registry = ModelRegistry([CanonicalModel("qualification-model", frozenset(), capabilities)],
                             [mapping])
    adapter = OpenAICompatibleAdapter(ProviderConfig(
        id=str(PROVIDER), name="synthetic", base_url="http://127.0.0.1",
        protocol=protocol, capabilities=capabilities,
    ))
    runtime = GatewayRuntime(
        key_store=InMemoryGatewayKeyStore(keys, clients), key_hasher=hasher,
        model_registry=registry, routing_engine=RoutingEngine(registry),
        provider_states=(ProviderState(str(PROVIDER)),),
        credential_states=(CredentialState(str(CREDENTIAL), str(PROVIDER)),),
        provider_model_adapters={str(MAPPING): adapter},
        credentials={str(CREDENTIAL): Credential(id=str(CREDENTIAL), secret="synthetic")},
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False),
        route_controls=RouteControls({str(MAPPING): 2}),
    )
    return runtime, issued


async def cleanup_mixed(pool, clients):
    for client in clients:
        await pool.execute(
            "delete from public.settlement_outbox where reservation_id in "
            "(select id from public.financial_reservations where client_id=$1)", client,
        )
        await pool.execute("delete from public.financial_reservations where client_id=$1", client)
        await pool.execute("delete from public.workflow_jobs where client_id=$1", client)
        await pool.execute("delete from public.request_logs where client_id=$1", client)
        await pool.execute("delete from public.gateway_clients where id=$1", client)
    scopes = [prefix + scope for prefix in ("", "workflow-share:") for scope in (
        f"route:{MAPPING}", f"provider:{PROVIDER}", f"credential:{CREDENTIAL}",
    )]
    scopes += ["interactive:global", "interactive:streams"]
    scopes += [f"interactive:client:{client}" for client in clients]
    await pool.execute("delete from public.capacity_leases where scope=any($1::text[])", scopes)
    await pool.execute("delete from public.capacity_scopes where scope=any($1::text[])", scopes)
    await pool.execute("delete from public.route_circuits where route_id=$1", str(MAPPING))
