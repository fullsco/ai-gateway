from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from gateway.app import create_app
from gateway.config import Settings
from tests.test_messages_api import make_runtime


@pytest.mark.asyncio
async def test_workflow_submission_requires_auth_permission_and_durable_acceptance(monkeypatch):
    runtime, key = make_runtime(lambda _: None)
    now = datetime.now(UTC)
    store = AsyncMock()
    store.submit.return_value = {"id": uuid4(), "status": "queued", "version": 0,
        "checkpoint": {}, "result": None, "deadline": now + timedelta(hours=1),
        "created_at": now, "updated_at": now}
    monkeypatch.setattr("gateway.api.workflows.WorkflowStore", lambda *args, **kwargs: store)
    app = create_app(Settings(_env_file=None, workflows_enabled=True), runtime, db_pool=object())
    body = {"protocol": "anthropic_messages", "request": {"model": "model-x"}}
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                    base_url="http://test") as client:
            assert (await client.post("/v1/workflows", json=body)).status_code == 401
            headers = {"x-api-key": key, "idempotency-key": "synthetic"}
            response = await client.post("/v1/workflows", headers=headers, json=body)
            assert response.status_code == 202
            assert store.submit.call_args.args[0] == "client-1"
            denied = {**body, "protocol": "openai_responses"}
            denied_response = await client.post("/v1/workflows", headers=headers, json=denied)
            assert denied_response.status_code == 403
            store.submit.assert_awaited_once()
            store.get.return_value = None
            missing = await client.get(f"/v1/workflows/{uuid4()}", headers=headers)
            assert missing.status_code == 404
    finally:
        await runtime.http_client.aclose()
