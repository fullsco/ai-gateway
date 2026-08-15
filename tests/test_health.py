from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.config import Settings


def make_client(**settings) -> TestClient:
    app = create_app(Settings(environment="test", log_format="json", _env_file=None, **settings))
    return TestClient(app)


def test_health_and_readiness() -> None:
    with make_client() as client:
        health = client.get("/health")
        ready = client.get("/ready")

    assert health.status_code == 200
    assert health.text == "ok"
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready"}
    assert health.headers["x-request-id"].startswith("gw_")


def test_untrusted_incoming_request_id_is_replaced() -> None:
    with make_client() as client:
        response = client.get("/health", headers={"x-request-id": "attacker-value"})

    assert response.headers["x-request-id"] != "attacker-value"


def test_valid_incoming_request_id_can_be_trusted_explicitly() -> None:
    request_id = "gw_019c00000000_0123456789abcdef"
    with make_client(trust_incoming_request_id=True) as client:
        response = client.get("/health", headers={"x-request-id": request_id})

    assert response.headers["x-request-id"] == request_id
