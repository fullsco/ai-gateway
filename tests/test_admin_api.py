from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.config import Settings


def test_admin_api_requires_authentication() -> None:
    app = create_app(Settings(environment="test", _env_file=None))

    with TestClient(app) as client:
        response = client.get("/api/admin/v1/overview")

    assert response.status_code == 401
    assert response.json() == {"error": "admin_authentication_required"}
