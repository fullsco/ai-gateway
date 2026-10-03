"""Committed workflow protocol matrix; run serially through dev_db_check --isolated.

Uses the HTTP/worker/synthetic-provider setup from test_workflow_e2e_remote. Only
provider I/O is mocked. Existing rows are never reset, reseeded, or deleted.
"""

import json
from decimal import Decimal
from itertools import product
from uuid import UUID, uuid4

import httpx
import pytest

from gateway.app import create_app
from gateway.auth import ClientPermissions, GatewayClient, InMemoryGatewayKeyStore
from gateway.config import Settings
from gateway.models import CanonicalModel, ModelRegistry, ProviderModel
from gateway.protocols import Capability, ClientProtocol
from gateway.protocols.translate import get_translator
from gateway.providers import Credential, ProviderConfig
from gateway.providers.anthropic import AnthropicCompatibleAdapter
from gateway.providers.openai import OpenAICompatibleAdapter
from gateway.providers.translating import TranslatingAdapter
from gateway.routing import CredentialState, ProviderState, RoutingEngine
from gateway.runtime import GatewayRuntime
from gateway.security.gateway_keys import GatewayKeyHasher
from gateway.workflow_worker import WorkflowWorker
from scripts.seed_dbtest import CREDENTIAL, MAPPING, PROVIDER
from tests.test_remote_isolation import dedicated_pool

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

CHAT = ClientProtocol.OPENAI_CHAT_COMPLETIONS
ANTHROPIC = ClientProtocol.ANTHROPIC_MESSAGES
RESPONSES = ClientProtocol.OPENAI_RESPONSES
PROTOCOLS = (CHAT, ANTHROPIC, RESPONSES)
LABELS = {CHAT: "chat", ANTHROPIC: "anthropic", RESPONSES: "responses"}
PATHS = {CHAT: "/v1/chat/completions", ANTHROPIC: "/v1/messages", RESPONSES: "/v1/responses"}
CAPABILITIES = frozenset({Capability.TOOL_CALLING})
MODEL = "qualification-model"
PROMPT = "Compare the weather in Paris and Rome and give the timezone for Paris."
FINAL = "Paris: 18 C; Rome: 24 C. Paris timezone: Europe/Paris."
CALLS = [
    ("call_weather_paris", "lookup_weather", {"city": "Paris"}),
    ("call_weather_rome", "lookup_weather", {"city": "Rome"}),
    ("call_timezone_paris", "lookup_timezone", {"city": "Paris"}),
]
RESULTS = [
    {"city": "Paris", "temperature_c": 18},
    {"city": "Rome", "temperature_c": 24},
    {"city": "Paris", "timezone": "Europe/Paris"},
]
SCHEMA = {"type": "object", "properties": {"city": {"type": "string"}},
          "required": ["city"], "additionalProperties": False}
PRICING = {"input_per_million": "1", "output_per_million": "2",
           "cached_input_per_million": "0.25", "currency": "USD"}
# Inclusive input, output, cache read, settled USD: independently calculated fixtures.
USAGE = [(120, 45, 20, Decimal("0.00019500")),
         (210, 32, 30, Decimal("0.00025150"))]


def tool_definitions(protocol):
    definitions = []
    for name in ("lookup_weather", "lookup_timezone"):
        definition = {"name": name, "description": f"Look up {name.removeprefix('lookup_')}."}
        definition["input_schema" if protocol == ANTHROPIC else "parameters"] = SCHEMA
        if protocol == CHAT:
            definition = {"type": "function", "function": definition}
        elif protocol == RESPONSES:
            definition = {"type": "function", **definition}
        definitions.append(definition)
    return definitions


def request_body(protocol):
    body = {"model": MODEL, "stream": False, "tools": tool_definitions(protocol)}
    if protocol == RESPONSES:
        # Exercise durable conversion from a string input into a full tool transcript.
        body.update(input=PROMPT, max_output_tokens=256)
    else:
        body.update(messages=[{"role": "user", "content": PROMPT}], max_tokens=256)
    return body


def wire_calls(protocol):
    if protocol == ANTHROPIC:
        return [{"type": "tool_use", "id": identifier, "name": name, "input": arguments}
                for identifier, name, arguments in CALLS]
    if protocol == RESPONSES:
        # Item IDs intentionally differ from invocation IDs: results must use call_id.
        return [{"type": "function_call", "id": f"fc_{index}", "call_id": identifier,
                 "name": name, "arguments": json.dumps(arguments), "status": "completed"}
                for index, (identifier, name, arguments) in enumerate(CALLS)]
    return [{"id": identifier, "type": "function",
             "function": {"name": name, "arguments": json.dumps(arguments)}}
            for identifier, name, arguments in CALLS]


def provider_response(protocol, turn):
    input_tokens, output_tokens, cached, _ = USAGE[turn]
    calls = wire_calls(protocol) if turn == 0 else []
    if protocol == ANTHROPIC:
        return {"id": f"msg_synthetic_{turn}", "type": "message", "role": "assistant",
                "model": "synthetic", "content": calls or [{"type": "text", "text": FINAL}],
                "stop_reason": "tool_use" if calls else "end_turn", "stop_sequence": None,
                "usage": {"input_tokens": input_tokens - cached, "output_tokens": output_tokens,
                          "cache_read_input_tokens": cached, "cache_creation_input_tokens": 0}}
    if protocol == RESPONSES:
        return {"id": f"resp_synthetic_{turn}", "object": "response", "status": "completed",
                "model": "synthetic", "output": calls or [
                    {"id": "msg_final", "type": "message", "role": "assistant",
                     "status": "completed", "content": [
                         {"type": "output_text", "text": FINAL, "annotations": []}]}],
                "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens,
                          "total_tokens": input_tokens + output_tokens,
                          "input_tokens_details": {"cached_tokens": cached}}}
    message = {"role": "assistant", "content": None if calls else FINAL}
    if calls:
        message["tool_calls"] = calls
    return {"id": f"chatcmpl_synthetic_{turn}", "object": "chat.completion",
            "created": 1, "model": "synthetic", "choices": [
                {"index": 0, "message": message,
                 "finish_reason": "tool_calls" if calls else "stop"}],
            "usage": {"prompt_tokens": input_tokens, "completion_tokens": output_tokens,
                      "total_tokens": input_tokens + output_tokens,
                      "prompt_tokens_details": {"cached_tokens": cached}}}


def decoded_calls(protocol, calls):
    if protocol == ANTHROPIC:
        return [(call["id"], call["name"], call["input"]) for call in calls]
    if protocol == RESPONSES:
        return [(call["call_id"], call["name"], json.loads(call["arguments"])) for call in calls]
    return [(call["id"], call["function"]["name"], json.loads(call["function"]["arguments"]))
            for call in calls]


def assert_transcript(protocol, payload, result_count):
    """Independent wire assertions, never using production translators as the oracle."""
    if protocol == RESPONSES:
        items = payload["input"]
        assert isinstance(items, list)
        calls = [item for item in items if item.get("type") == "function_call"]
        outputs = [item for item in items if item.get("type") == "function_call_output"]
        results = [(item["call_id"], json.loads(item["output"])) for item in outputs]
        assert max(items.index(call) for call in calls) < min(
            (items.index(item) for item in outputs), default=len(items))
    elif protocol == ANTHROPIC:
        messages = payload["messages"]
        calls, outputs = [], []
        saw_result = False
        for message in messages:
            content = message["content"]
            for block in content if isinstance(content, list) else []:
                if block["type"] == "tool_use":
                    assert message["role"] == "assistant" and not saw_result
                    calls.append(block)
                elif block["type"] == "tool_result":
                    assert message["role"] == "user"
                    saw_result = True
                    outputs.append(block)
        results = [(block["tool_use_id"], json.loads(block["content"])) for block in outputs]
    else:
        messages = payload["messages"]
        assistants = [message for message in messages if message.get("tool_calls")]
        assert len(assistants) == 1, "Parallel calls must share one assistant turn before results"
        calls = assistants[0]["tool_calls"]
        outputs = messages[messages.index(assistants[0]) + 1:]
        assert all(message["role"] == "tool" for message in outputs)
        results = [(message["tool_call_id"], json.loads(message["content"])) for message in outputs]
    assert decoded_calls(protocol, calls) == CALLS
    assert results == [(call[0], value) for call, value in zip(CALLS, RESULTS, strict=True)][
        :result_count]
    assert PROMPT in json.dumps(payload, ensure_ascii=False)


async def assert_settlement(pool, client_id, job_id, client_protocol, upstream_protocol, turns):
    expected = USAGE[:turns]
    total_cost = sum((row[3] for row in expected), Decimal(0))
    total_tokens = sum(row[0] + row[1] for row in expected)
    job = await pool.fetchrow(
        "select reserved_cost,reserved_tokens from public.workflow_jobs where id=$1", job_id)
    assert (job["reserved_cost"], job["reserved_tokens"]) == (total_cost, total_tokens)
    assert await pool.fetchval(
        "select sum(reserved_cost) from public.client_spend_windows where client_id=$1",
        client_id) == total_cost
    reservations = await pool.fetch(
        "select f.*,o.actual_cost as outbox_cost,o.applied_at from public.financial_reservations f "
        "left join public.settlement_outbox o on o.reservation_id=f.id "
        "where f.client_id=$1 order by f.created_at,f.id", client_id)
    assert len(reservations) == turns
    assert len({row["telemetry_request_id"] for row in reservations}) == turns
    for reservation, (_, _, _, cost) in zip(reservations, expected, strict=True):
        assert reservation["workflow_id"] == job_id
        assert reservation["status"] == "settled"
        assert reservation["attempt_number"] == 1
        assert reservation["actual_cost"] == reservation["outbox_cost"] == cost
        assert reservation["applied_at"] is not None
    logs = await pool.fetch(
        "select status,protocol,retry_count,fallback_count from public.request_logs "
        "where client_id=$1", client_id)
    assert [tuple(row) for row in logs] == [("succeeded", client_protocol.value, 0, 0)] * turns
    attempts = await pool.fetch(
        "select a.status,a.upstream_status,a.attempt_number,a.provider_id,a.credential_id,"
        "a.provider_model_id from public.request_attempts a join public.request_logs r "
        "on r.id=a.request_id where r.client_id=$1", client_id)
    assert [tuple(row) for row in attempts] == [
        ("succeeded", 200, 1, PROVIDER, CREDENTIAL, MAPPING)] * turns
    usage = await pool.fetch(
        "select u.input_tokens,u.output_tokens,u.cached_tokens,u.estimated_cost,u.currency,"
        "u.protocol_snapshot,u.attempt_status_snapshot,u.canonical_model_snapshot,"
        "u.upstream_model_snapshot from public.usage_records u join public.request_logs r "
        "on r.id=u.request_id where r.client_id=$1 order by r.started_at,r.id", client_id)
    assert [tuple(row) for row in usage] == [
        (*row, "USD", upstream_protocol.value, "succeeded", MODEL, "synthetic") for row in expected]


@pytest.mark.parametrize("client_protocol,upstream_protocol", [
    pytest.param(client, upstream, id=f"{LABELS[client]}-to-{LABELS[upstream]}")
    for client, upstream in product(PROTOCOLS, repeat=2)
])
async def test_protocol_matrix_multi_tool_workflow(client_protocol, upstream_protocol):
    translator = get_translator(client_protocol, upstream_protocol)
    if client_protocol != upstream_protocol:
        assert translator is not None, "Translation support requires a production code fix"
    async with dedicated_pool() as pool:
        # run_once recovers/claims globally. Refuse to touch a project containing live
        # user work; never clean it up to make scheduling assertions pass. Run serially.
        assert not await pool.fetchval(
            "select exists(select 1 from public.workflow_jobs where status in "
            "('queued','running','waiting_for_tool','waiting_for_approval'))"
        ), "Existing active workflows must be preserved; use an idle dedicated test project"
        # Shared budgets would mutate somebody else's spend windows even with a fresh client.
        assert not await pool.fetchval(
            "select exists(select 1 from public.gateway_budgets where enabled and "
            "currency='USD' and (scope_type='global' or "
            "(scope_type='provider' and scope_id=$1) or "
            "(scope_type='credential' and scope_id=$2) or "
            "(scope_type='model' and scope_id=$3)))", str(PROVIDER), str(CREDENTIAL), MODEL
        ), "Existing matching budgets must be preserved; use an unbudgeted synthetic route"
        client_id = uuid4()
        runtime = None
        requests, wire_errors = [], []

        def provider(request):
            # Capture assertion details: the worker deliberately catches provider errors.
            try:
                turn = len(requests)
                requests.append(request)
                assert turn < 2, "Tools must not cause extra inference dispatches"
                assert request.method == "POST"
                assert str(request.url) == f"http://127.0.0.1{PATHS[upstream_protocol]}"
                sent = json.loads(request.content)
                assert sent["stream"] is False
                assert sent["model"] == "synthetic"
                assert sent["tools"] == tool_definitions(upstream_protocol)
                ceiling = "max_output_tokens" if upstream_protocol == RESPONSES else "max_tokens"
                assert sent[ceiling] == 256
                if turn:
                    assert_transcript(upstream_protocol, sent, len(CALLS))
                else:
                    assert PROMPT in json.dumps(sent)
                return httpx.Response(200, json=provider_response(upstream_protocol, turn))
            except AssertionError as exc:
                wire_errors.append(str(exc))
                raise

        try:
            await pool.execute(
                "insert into public.gateway_clients(id,name,spending_limit) "
                "values($1,'Synthetic workflow protocol matrix',10)", client_id)
            hasher = GatewayKeyHasher(b"s" * 32)
            issued = hasher.issue(key_id="synthetic", client_id=str(client_id))
            client = GatewayClient(str(client_id), "synthetic",
                                   ClientPermissions(frozenset({client_protocol})))
            mapping = ProviderModel(
                str(MAPPING), MODEL, str(PROVIDER), "synthetic", upstream_protocol, CAPABILITIES,
                pricing=PRICING, serves_protocols=frozenset({client_protocol}))
            registry = ModelRegistry([CanonicalModel(MODEL, frozenset(), CAPABILITIES)], [mapping])
            adapter_type = AnthropicCompatibleAdapter if upstream_protocol == ANTHROPIC \
                else OpenAICompatibleAdapter
            adapter = adapter_type(ProviderConfig(
                id=str(PROVIDER), name="SyntheticProvider", base_url="http://127.0.0.1",
                protocol=upstream_protocol, capabilities=CAPABILITIES))
            runtime = GatewayRuntime(
                key_store=InMemoryGatewayKeyStore([issued.record], [client]), key_hasher=hasher,
                model_registry=registry, routing_engine=RoutingEngine(registry),
                provider_states=(ProviderState(str(PROVIDER)),),
                credential_states=(CredentialState(str(CREDENTIAL), str(PROVIDER)),),
                provider_model_adapters={str(MAPPING): adapter},
                translating_adapters={
                    (str(MAPPING), client_protocol): TranslatingAdapter(adapter, translator)
                } if translator else {},
                credentials={str(CREDENTIAL): Credential(id=str(CREDENTIAL), secret="synthetic")},
                http_client=httpx.AsyncClient(
                    transport=httpx.MockTransport(provider), trust_env=False))
            settings = Settings(
                _env_file=None, database_url=None, environment="test", workflows_enabled=True,
                durable_accounting_enabled=True, distributed_admission_enabled=False,
                health_probe_enabled=False, alert_monitor_enabled=False,
                credential_usage_poll_enabled=False, failover_enabled=False, trace_sample_rate=0)
            app = create_app(settings, runtime, db_pool=pool)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                        base_url="http://test") as http:
                headers = {"Authorization": f"Bearer {issued.plaintext}",
                           "Idempotency-Key": str(uuid4())}
                submitted = await http.post("/v1/workflows", headers=headers, json={
                    "protocol": client_protocol.value, "request": request_body(client_protocol),
                    "max_steps": 2, "max_tool_calls": len(CALLS), "max_cost": "1",
                    "max_total_tokens": 8192})
                assert submitted.status_code == 202, submitted.text
                job_id = UUID(submitted.json()["id"])
                path = f"/v1/workflows/{job_id}"
                assert submitted.json()["status"] == "queued"
                assert submitted.json()["checkpoint"] == {}
                assert await WorkflowWorker(pool, runtime, settings).run_once()
                for index, (invocation, _, _) in enumerate(CALLS):
                    waiting = await http.get(path, headers=headers)
                    assert waiting.status_code == 200, waiting.text
                    state = waiting.json()
                    assert not wire_errors, wire_errors
                    assert state["status"] == "waiting_for_tool", state
                    assert state["version"] == 2 + 3 * index
                    assert state["result"] is None
                    checkpoint = state["checkpoint"]
                    assert checkpoint["step"] == 1
                    assert checkpoint["tool_calls"] == len(CALLS)
                    assert checkpoint["invocation_id"] == invocation
                    assert decoded_calls(client_protocol, checkpoint["pending_tools"]) == (
                        CALLS[index:])
                    assert decoded_calls(client_protocol, [checkpoint["tool"]]) == [CALLS[index]]
                    assert "tool_result" not in checkpoint
                    assert_transcript(client_protocol, checkpoint["request"], index)
                    assert len(requests) == 1
                    await assert_settlement(pool, client_id, job_id, client_protocol,
                                            upstream_protocol, 1)
                    accepted = await http.post(f"{path}/tool-results", headers=headers, json={
                        "invocation_id": invocation, "result": RESULTS[index]})
                    assert accepted.status_code == 200, accepted.text
                    assert accepted.json()["status"] == "queued"
                    assert accepted.json()["version"] == state["version"] + 1
                    # New worker owner each time: continuation comes from committed storage.
                    assert await WorkflowWorker(pool, runtime, settings).run_once()
                completed = await http.get(path, headers=headers)
                assert completed.status_code == 200, completed.text
                state = completed.json()
                assert not wire_errors, wire_errors
                assert state["status"] == "completed", state
                assert state["version"] == 11
                assert state["checkpoint"] == {"step": 2}
                assert state["reserved_tokens"] == 407
                assert Decimal(str(state["reserved_cost"])) == Decimal("0.00044650")
                result = state["result"]
                usage = result["usage"]
                if client_protocol == ANTHROPIC:
                    assert result["type"] == "message" and result["stop_reason"] == "end_turn"
                    assert result["content"] == [{"type": "text", "text": FINAL}]
                    assert (usage["input_tokens"], usage["output_tokens"],
                            usage["cache_read_input_tokens"]) == (180, 32, 30)
                elif client_protocol == RESPONSES:
                    assert result["object"] == "response" and result["status"] == "completed"
                    assert len(result["output"]) == 1
                    assert result["output"][0]["type"] == "message"
                    assert result["output"][0]["content"][0]["text"] == FINAL
                    assert (usage["input_tokens"], usage["output_tokens"], usage["total_tokens"],
                            usage["input_tokens_details"]["cached_tokens"]) == (210, 32, 242, 30)
                else:
                    assert result["object"] == "chat.completion"
                    assert len(result["choices"]) == 1
                    assert result["choices"][0]["finish_reason"] == "stop"
                    assert result["choices"][0]["message"] == {
                        "role": "assistant", "content": FINAL}
                    assert (usage["prompt_tokens"], usage["completion_tokens"],
                            usage["total_tokens"],
                            usage["prompt_tokens_details"]["cached_tokens"]) == (210, 32, 242, 30)
                assert len(requests) == 2
                await assert_settlement(pool, client_id, job_id, client_protocol,
                                        upstream_protocol, 2)
                persisted = await pool.fetchrow(
                    "select attempts,lease_owner,lease_expires_at from public.workflow_jobs "
                    "where id=$1", job_id)
                assert tuple(persisted) == (4, None, None)
                tools = await pool.fetch(
                    "select invocation_id,result from public.workflow_tool_results "
                    "where job_id=$1", job_id)
                assert {row["invocation_id"]: json.loads(row["result"]) for row in tools} == {
                    call[0]: value for call, value in zip(CALLS, RESULTS, strict=True)}
                events = await pool.fetch(
                    "select sequence,kind from public.workflow_events where job_id=$1 "
                    "order by sequence", job_id)
                kinds = ["queued", "running", "waiting_for_tool"]
                kinds += ["tool_result_received", "running", "waiting_for_tool"] * 2
                kinds += ["tool_result_received", "running", "completed"]
                assert [tuple(row) for row in events] == list(enumerate(kinds))
        finally:
            if runtime:
                await runtime.http_client.aclose()
            await pool.execute(
                "delete from public.settlement_outbox where reservation_id in "
                "(select id from public.financial_reservations where client_id=$1)", client_id)
            await pool.execute("delete from public.financial_reservations where client_id=$1",
                               client_id)
            await pool.execute("delete from public.workflow_jobs where client_id=$1", client_id)
            await pool.execute("delete from public.request_logs where client_id=$1", client_id)
            await pool.execute("delete from public.gateway_clients where id=$1", client_id)
