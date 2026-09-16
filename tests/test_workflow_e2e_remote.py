"""Real HTTP handlers, worker and accounting with remote storage and mocked provider I/O."""

import asyncio
import base64
import json
from contextlib import AsyncExitStack
from dataclasses import replace
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import pytest

from gateway.app import create_app
from gateway.auth import ClientPermissions, GatewayClient, InMemoryGatewayKeyStore
from gateway.config import Settings
from gateway.models import CanonicalModel, ModelRegistry, ProviderModel
from gateway.protocols import ClientProtocol
from gateway.providers import Credential, ProviderConfig
from gateway.providers.anthropic import AnthropicCompatibleAdapter
from gateway.providers.openai import OpenAICompatibleAdapter
from gateway.routing import CredentialState, ProviderState, RoutingEngine
from gateway.runtime import GatewayRuntime
from gateway.security.gateway_keys import GatewayKeyHasher
from gateway.workflow_worker import WorkflowWorker
from scripts.seed_dbtest import CREDENTIAL, MAPPING, PROVIDER
from tests.test_remote_isolation import dedicated_pool

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.mark.parametrize("mode", [
    "single", "tools", "budget", "coordinated", "artifact", "ambiguous", "large_tools",
    "messages_single", "responses_single",
    "combined",
])
async def test_http_submission_to_worker_completion_has_durable_usage_and_settlement(mode):
    async with dedicated_pool() as pool:
        client_id = uuid4()
        job_id = None
        runtime = None
        protocol = ClientProtocol.OPENAI_CHAT_COMPLETIONS
        if mode == "messages_single":
            protocol = ClientProtocol.ANTHROPIC_MESSAGES
        elif mode == "responses_single":
            protocol = ClientProtocol.OPENAI_RESPONSES
        requests = []
        lifetime = AsyncExitStack()

        def provider(request):
            assert request.url.host == "127.0.0.1"
            requests.append(request)
            if mode == "ambiguous":
                raise httpx.ReadTimeout("Synthetic accepted request with unknown completion")
            if protocol == ClientProtocol.ANTHROPIC_MESSAGES:
                return httpx.Response(200, json={"id": "synthetic", "type": "message",
                    "role": "assistant", "content": [{"type": "text", "text": "ok"}],
                    "stop_reason": "end_turn", "usage": {"input_tokens": 4, "output_tokens": 1}})
            if protocol == ClientProtocol.OPENAI_RESPONSES:
                return httpx.Response(200, json={"id": "synthetic", "object": "response",
                    "status": "completed", "output": [{"type": "message", "role": "assistant",
                        "content": [{"type": "output_text", "text": "ok"}]}],
                    "usage": {"input_tokens": 4, "output_tokens": 1}})
            if mode in {"tools", "budget", "large_tools"} and len(requests) == 1:
                return httpx.Response(200, json={
                    "id": "synthetic", "object": "chat.completion", "model": "synthetic",
                    "choices": [{"index": 0, "message": {"role": "assistant", "tool_calls": [
                        {"id": f"tool-{index}", "type": "function",
                         "function": {"name": "synthetic", "arguments": "{}"}}
                        for index in range(4)
                    ]}, "finish_reason": "tool_calls"}],
                    "usage": {"prompt_tokens": 4, "completion_tokens": 1},
                })
            if len(requests) == 2:
                sent = json.loads(request.content)
                assert [m["tool_call_id"] for m in sent["messages"] if m["role"] == "tool"] == [
                    f"tool-{index}" for index in range(4)
                ]
            return httpx.Response(200, json={
                "id": "synthetic", "object": "chat.completion", "model": "synthetic",
                "choices": [{"index": 0, "message": {"role": "assistant",
                             "content": "x" * 300000 if mode == "artifact" else "ok"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5},
            })

        try:
            await pool.execute(
                "insert into public.gateway_clients(id,name,spending_limit) "
                "values($1,'Synthetic workflow E2E',10)", client_id,
            )
            hasher = GatewayKeyHasher(b"s" * 32)
            issued = hasher.issue(key_id="synthetic", client_id=str(client_id))
            client = GatewayClient(str(client_id), "synthetic",
                                   ClientPermissions(frozenset({protocol})))
            mapping = ProviderModel(str(MAPPING), "qualification-model", str(PROVIDER),
                                    "synthetic", protocol, frozenset(),
                                    pricing={"per_request": "0.25", "currency": "USD"})
            registry = ModelRegistry([CanonicalModel("qualification-model", frozenset(),
                                                       frozenset())], [mapping])
            adapter_type = AnthropicCompatibleAdapter if mode == "messages_single" \
                else OpenAICompatibleAdapter
            adapter = adapter_type(ProviderConfig(
                id=str(PROVIDER), name="SyntheticProvider", base_url="http://127.0.0.1",
                protocol=protocol, capabilities=frozenset(),
            ))
            runtime = GatewayRuntime(
                key_store=InMemoryGatewayKeyStore([issued.record], [client]), key_hasher=hasher,
                model_registry=registry, routing_engine=RoutingEngine(registry),
                provider_states=(ProviderState(str(PROVIDER)),),
                credential_states=(CredentialState(str(CREDENTIAL), str(PROVIDER)),),
                provider_model_adapters={str(MAPPING): adapter},
                credentials={str(CREDENTIAL): Credential(id=str(CREDENTIAL), secret="synthetic")},
                http_client=httpx.AsyncClient(
                    transport=httpx.MockTransport(provider), trust_env=False,
                ),
            )
            settings = Settings(_env_file=None, database_url=None, workflows_enabled=True,
                                durable_accounting_enabled=True, health_probe_enabled=False,
                                alert_monitor_enabled=False, credential_usage_poll_enabled=False,
                                distributed_admission_enabled=mode == "coordinated",
                                artifact_encryption_key=base64.b64encode(b"a" * 32).decode())
            app = create_app(settings, runtime, db_pool=pool)
            if mode == "combined":
                await lifetime.enter_async_context(app.router.lifespan_context(app))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                        base_url="http://test") as http:
                headers = {"Authorization": f"Bearer {issued.plaintext}",
                           "Idempotency-Key": str(uuid4())}
                request_body = {"model": "qualification-model", "messages": [], "max_tokens": 16}
                if mode == "responses_single":
                    request_body = {"model": "qualification-model", "input": "synthetic",
                                    "max_output_tokens": 16}
                result = await http.post("/v1/workflows", headers=headers, json={
                    "protocol": protocol.value,
                    "max_cost": "0.30" if mode == "budget" else "10",
                    "max_total_tokens": 300000 if mode == "large_tools" else 131072,
                    "request": request_body,
                })
                assert result.status_code == 202, result.text
                job_id = result.json()["id"]
                worker = WorkflowWorker(pool, runtime, settings)
                if mode == "combined":
                    async with asyncio.timeout(90):
                        while True:
                            state = await http.get(f"/v1/workflows/{job_id}", headers=headers)
                            if state.json()["status"] in {"completed", "failed"}:
                                break
                            await asyncio.sleep(.5)
                else:
                    assert await worker.run_once()
                if mode in {"tools", "budget", "large_tools"}:
                    for index in range(4):
                        waiting = await http.get(f"/v1/workflows/{job_id}", headers=headers)
                        assert waiting.json()["status"] == "waiting_for_tool", waiting.text
                        assert waiting.json()["checkpoint"]["invocation_id"] == f"tool-{index}"
                        tool_value = "ok"
                        if mode == "large_tools":
                            uploaded = await http.post(
                                f"/v1/workflows/{job_id}/artifacts", headers=headers,
                                content=b"x" * 140000,
                            )
                            assert uploaded.status_code == 201, uploaded.text
                            tool_value = {"artifact_id": uploaded.json()["id"]}
                        accepted = await http.post(
                            f"/v1/workflows/{job_id}/tool-results", headers=headers,
                            json={"invocation_id": f"tool-{index}", "result": tool_value},
                        )
                        assert accepted.status_code == 200, accepted.text
                        assert await worker.run_once()
                completed = await http.get(f"/v1/workflows/{job_id}", headers=headers)
                if mode == "ambiguous":
                    assert completed.json()["status"] == "waiting_for_approval"
                    assert len(requests) == 1
                    assert await pool.fetchval(
                        "select status from public.financial_reservations where workflow_id=$1",
                        UUID(job_id),
                    ) == "unknown"
                    return
                if mode == "budget":
                    assert completed.json()["status"] == "waiting_for_approval", completed.text
                    assert len(requests) == 1
                    assert completed.json()["reserved_cost"] == .25
                    return
                assert completed.json()["status"] == "completed", completed.text
                if mode == "artifact":
                    reference = completed.json()["result"]["artifact"]
                    downloaded = await http.get(
                        f"/v1/workflows/{job_id}/artifacts/{reference['id']}", headers=headers,
                    )
                    assert downloaded.status_code == 200
                    assert downloaded.json()["choices"][0]["message"]["content"] == "x" * 300000
                elif mode == "messages_single":
                    assert completed.json()["result"]["content"][0]["text"] == "ok"
                elif mode == "responses_single":
                    assert completed.json()["result"]["output"][0]["content"][0]["text"] == "ok"
                else:
                    assert completed.json()["result"]["choices"][0]["message"]["content"] == "ok"
                expected_requests = 2 if mode in {"tools", "large_tools"} else 1
                assert len(requests) == expected_requests
                if mode != "combined":
                    assert not await worker.run_once()
                # Tenant authorization also applies to a valid but unrelated key.
                other = replace(client, id=str(uuid4()))
                other_key = hasher.issue(key_id="other", client_id=other.id)
                runtime.key_store._clients[other.id] = other
                runtime.key_store._keys[other_key.record.key_prefix] = other_key.record
                denied = await http.get(f"/v1/workflows/{job_id}",
                    headers={"Authorization": f"Bearer {other_key.plaintext}"})
                assert denied.status_code == 404
            assert await pool.fetchval(
                "select count(*) from public.financial_reservations "
                "where client_id=$1 and status='settled'", client_id,
            ) == expected_requests
            assert await pool.fetchval(
                "select reserved_cost from public.client_spend_windows where client_id=$1",
                client_id,
            ) == Decimal("0.25") * expected_requests
            assert await pool.fetchval(
                "select count(*) from public.usage_records u join public.request_logs r "
                "on r.id=u.request_id where r.client_id=$1", client_id,
            ) == expected_requests
        finally:
            await lifetime.aclose()
            if runtime:
                await runtime.http_client.aclose()
            await pool.execute("delete from public.settlement_outbox where reservation_id in "
                               "(select id from public.financial_reservations where client_id=$1)",
                               client_id)
            await pool.execute("delete from public.financial_reservations where client_id=$1",
                               client_id)
            if job_id:
                await pool.execute("delete from public.workflow_jobs where id=$1", UUID(job_id))
            await pool.execute("delete from public.request_logs where client_id=$1", client_id)
            await pool.execute("delete from public.gateway_clients where id=$1", client_id)
            if mode == "coordinated":
                await pool.execute("delete from public.route_circuits where route_id=$1",
                                   str(MAPPING))
                await pool.execute("delete from public.capacity_scopes where scope=any($1::text[])",
                    [f"{prefix}{scope}" for prefix in ("", "workflow-share:")
                     for scope in (f"route:{MAPPING}", f"provider:{PROVIDER}",
                                   f"credential:{CREDENTIAL}")])
            if mode == "combined":
                await pool.execute(
                    "delete from public.capacity_leases where scope='workflow:global'"
                )
                await pool.execute(
                    "delete from public.capacity_scopes where scope='workflow:global'"
                )
