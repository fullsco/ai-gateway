import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]


def test_staging_compose_supervises_gateway_and_orders_tunnel_after_health() -> None:
    compose = yaml.safe_load((ROOT / "deploy/compose.staging.yml").read_text())
    gateway = compose["services"]["gateway"]
    tunnel = compose["services"]["tunnel"]

    assert gateway["restart"] == "unless-stopped"
    assert gateway["init"] is True
    assert gateway["ports"] == ["127.0.0.1:8320:8320"]
    assert tunnel["restart"] == "unless-stopped"
    assert tunnel["depends_on"]["gateway"] == {
        "condition": "service_healthy",
        "restart": True,
    }
    assert tunnel["secrets"] == ["tunnel_token"]
    assert "--token-file" in tunnel["command"]


def test_gateway_image_is_non_root_single_process_and_health_checked() -> None:
    dockerfile = (ROOT / "deploy/Dockerfile").read_text()

    assert "USER gateway" in dockerfile
    assert 'ENTRYPOINT ["python", "-m", "gateway"]' in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "--workers" not in dockerfile
    assert "COPY .env" not in dockerfile


def test_all_production_dependencies_are_exactly_pinned() -> None:
    requirements = (ROOT / "requirements.lock").read_text().splitlines()

    assert requirements
    assert all("==" in line for line in requirements if line and not line.startswith("#"))


def test_stream_qualification_never_contains_a_literal_gateway_key() -> None:
    script = (ROOT / "deploy/qualify_stream.py").read_text()

    assert "GATEWAY_SMOKE_KEY" in script
    assert "gw_live_" not in script
    assert 'print(key)' not in script


def test_active_deployment_qualification_has_bounded_failure_budget() -> None:
    script = (ROOT / "deploy/qualify_deployment.py").read_text()

    assert 'default=120' in script
    assert 'default=2' in script
    assert 'raise SystemExit(1)' in script


def test_qualification_requires_health_readiness_version_and_request_id() -> None:
    from deploy.qualify import http_check_passed

    passing = {"health": 200, "ready": 200, "version": 200, "request_id": True}
    assert http_check_passed(passing)
    for key, value in (
        ("health", 503),
        ("ready", 503),
        ("version", 500),
        ("request_id", False),
    ):
        failed = {**passing, key: value}
        assert not http_check_passed(failed)


def test_qualification_script_reports_local_mock_host(tmp_path: Path) -> None:
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "gateway.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            "18320",
            "--log-level",
            "error",
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "GATEWAY_DATABASE_URL": "",
            "GATEWAY_ENVIRONMENT": "test",
        },
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(120):
            try:
                import urllib.request

                urllib.request.urlopen("http://127.0.0.1:18320/ready", timeout=1)
                break
            except Exception:
                import time

                time.sleep(0.1)
        result = subprocess.run(
            [
                sys.executable,
                "deploy/qualify.py",
                "--local",
                "http://127.0.0.1:18320",
                "--public-host",
                "api.duedirect.info",
                "--concurrency",
                "4",
                "--skip-public",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        server.terminate()
        server.wait(timeout=10)

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["local"]["ready"] == 200
    assert report["public"] is None
    assert report["tls"] is None
    assert report["concurrency"] == {"requests": 4, "successful": 4}
