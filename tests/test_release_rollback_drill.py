from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from scripts import release_drill_infra
from scripts.release_support import PORTS, ReleaseIdentity, ingress_config, provenance, release_dsn


@pytest.mark.asyncio
async def test_retained_jobs_api_rejects_new_work_and_preserves_existing_job_operations():
    app = FastAPI()

    @app.api_route("/{path:path}", methods=["GET", "POST"])
    async def accepted(path):
        return {"accepted": path}

    transport = httpx.ASGITransport(ReleaseIdentity(app, "current", jobs_only=True))
    async with httpx.AsyncClient(transport=transport,
                                base_url="http://test", trust_env=False) as http:
        for path in ("/v1/workflows", "/v1/chat/completions", "/v1/responses", "/v1/messages"):
            assert (await http.post(path, json={})).status_code == 503
        for path in ("/v1/workflows/123", "/v1/workflows/123/tool-results",
                     "/v1/workflows/123/cancel", "/ready"):
            response = await http.post(path)
            assert response.status_code == 200
            assert response.headers["x-qualification-release"] == "current"


def test_ingress_switch_preserves_job_route_and_rejects_new_workflows():
    forward = ingress_config(Path("/tmp/fixture"))
    rollback = ingress_config(Path("/tmp/fixture"), True)
    assert f"listen 127.0.0.1:{PORTS['ingress']}" in rollback
    retained = f"location ^~ /v1/workflows/ {{ proxy_pass http://127.0.0.1:{PORTS['jobs']}; }}"
    assert retained in forward and retained in rollback
    assert "location = /v1/workflows { return 503; }" in rollback
    assert f"location / {{ proxy_pass http://127.0.0.1:{PORTS['baseline']}; }}" in rollback
    assert f"location / {{ proxy_pass http://127.0.0.1:{PORTS['current']}; }}" in forward
    assert "proxy_buffering off" in rollback


@pytest.mark.parametrize("suffix", ["gateway_qualification", "production", "postgres",
                                     "gateway_release_drill_bad"])
def test_disposable_db_guard_rejects_existing_databases(suffix):
    with pytest.raises(ValueError, match="own isolated database"):
        release_dsn(f"postgresql://gateway_qualification:fixture@127.0.0.1:5432/{suffix}")


def test_checkout_code_cannot_masquerade_as_installed_release():
    with pytest.raises(RuntimeError, match="installed wheel"):
        provenance({"versions": {}}, "current")


@pytest.mark.asyncio
async def test_ingress_handoff_waits_for_old_workers_to_release_listener(monkeypatch):
    phase = {"old_listening": True, "waits": 0}
    process = SimpleNamespace(pid=10, poll=lambda: None)
    monkeypatch.setattr(release_drill_infra, "worker_pids", lambda _: {11, 12})
    monkeypatch.setattr(release_drill_infra, "listening_sockets", lambda _: {"socket:[1]"})
    monkeypatch.setattr(release_drill_infra, "holds_listener",
                        lambda pid, _: pid == 12 or phase["old_listening"])

    async def advance(_):
        phase["waits"] += 1
        phase["old_listening"] = False

    monkeypatch.setattr(release_drill_infra.asyncio, "sleep", advance)
    result = await release_drill_infra.wait_for_handoff(process, {11}, 18520)
    assert phase["waits"] == 1
    assert result["old_listeners_closed"] and result["new_worker_listening"]
