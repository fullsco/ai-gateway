import json

import httpx
import pytest

from gateway.app import create_app
from gateway.config import Settings
from gateway.security.gateway_keys import GatewayKeyHasher
from scripts.qualification_report import validate_functional_report
from scripts.staging_service import make_runtime
from scripts.staging_translated import (
    CALLS,
    CHAT,
    PROTOCOLS,
    SCENARIOS,
    continuation,
    request_body,
    synthetic_frames,
    synthetic_response,
    validate_response,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol,stream", SCENARIOS)
async def test_translated_two_turn_journey(protocol, stream):
    sent = []

    class Stream(httpx.AsyncByteStream):
        def __init__(self, full):
            self.full = full

        async def __aiter__(self):
            for frame in synthetic_frames(self.full):
                yield frame

    def provider(request):
        assert request.url.path == "/v1/chat/completions"
        body = json.loads(request.content)
        sent.append(body)
        full = synthetic_response(body)
        if stream:
            return httpx.Response(200, stream=Stream(full),
                                  headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=full)

    issued = GatewayKeyHasher(b"s" * 32).issue(key_id="key", client_id="client")
    config = {"clients": {"interactive": {"id": "client", "key_id": "key",
        "prefix": issued.record.key_prefix, "digest": issued.record.digest}}}
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider),
                                 trust_env=False) as upstream:
        runtime = await make_runtime(True, config, upstream)
        app = create_app(Settings(_env_file=None, database_url=None, environment="test",
            health_probe_enabled=False, alert_monitor_enabled=False, alert_delivery_enabled=False,
            credential_usage_poll_enabled=False), runtime)
        from scripts.staging_translated import PATHS

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                    base_url="http://gateway.test", trust_env=False) as http:
            body = request_body(protocol, stream)
            headers = {"Authorization": "Bearer " + issued.plaintext}
            response = await http.post(PATHS[protocol], json=body, headers=headers)
            assistant = validate_response(protocol, response, stream)
            response = await http.post(PATHS[protocol], headers=headers,
                                       json=continuation(protocol, body, assistant))
            validate_response(protocol, response, stream, second=True)
    assert len(sent) == 2
    assert len(sent[-1]["messages"][1]["tool_calls"]) == len(CALLS)


@pytest.mark.asyncio
async def test_native_runtime_default_unchanged():
    async with httpx.AsyncClient(trust_env=False) as http:
        runtime = await make_runtime(config={"clients": {}}, http_client=http)
        assert runtime.translating_adapters == {}
        assert len(PROTOCOLS) == 3
        assert all(adapter.config.protocol == CHAT
                   for adapter in runtime.provider_model_adapters.values())


def test_gate_fails_absent_translated_journeys():
    soak = {"responses": {"200": 1}, "workflow_submissions": 1,
            "workflow_statuses": [{"status": "completed", "count": 1}],
            "successful_reservations_settled": True, "provider": {"active": 0, "peak": 1},
            "translated_coverage_required": True}
    report = {"workload_profile": "translated",
              "phases": {"mixed_soak": soak, "translated_coverage": {}}}
    with pytest.raises(ValueError, match="translated"):
        validate_functional_report(report)


def test_gate_rejects_partial_translated_coverage():
    report = {"workload_profile": "translated", "phases": {
        "mixed_soak": {"responses": {"200": 1}, "workflow_submissions": 1,
                       "workflow_statuses": [{"status": "completed", "count": 1}],
                       "successful_reservations_settled": True,
                       "provider": {"active": 0, "peak": 1},
                       "translated_coverage_required": True},
        "translated_coverage": {"openai_chat_completions/json_two_turn": {
            "started": 1, "completed": 1, "overloaded": 0, "failed": 0, "failures": []}}}}
    with pytest.raises(ValueError, match="translated"):
        validate_functional_report(report)


def test_gate_native_default_unchanged():
    report = {"workload_profile": "native", "phases": {"mixed_soak": {
        "responses": {"200": 1}, "workflow_submissions": 1,
        "workflow_statuses": [{"status": "completed", "count": 1}],
        "successful_reservations_settled": True, "provider": {"active": 0, "peak": 1}}}}
    assert validate_functional_report(report) is None


def test_gate_translated_passes_with_full_coverage():
    coverage = {f"{p.value}/{s}": {"started": 2, "completed": 2, "overloaded": 0,
                 "failed": 0, "failures": []}
                for p in PROTOCOLS for s in ("json_two_turn", "sse_two_turn", "workflow_two_turn")}
    report = {"workload_profile": "translated", "phases": {
        "mixed_soak": {"responses": {"200": 1}, "workflow_submissions": 1,
                       "workflow_statuses": [{"status": "completed", "count": 1}],
                       "successful_reservations_settled": True,
                       "provider": {"active": 0, "peak": 1},
                       "translated_coverage_required": True},
        "translated_coverage": coverage}}
    assert validate_functional_report(report) is None
