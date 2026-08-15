from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.auth import ClientPermissions, GatewayClient, InMemoryGatewayKeyStore
from gateway.config import Settings
from gateway.models import CanonicalModel, ModelRegistry
from gateway.protocols import ClientProtocol
from gateway.routing import RoutingEngine
from gateway.runtime import GatewayRuntime
from gateway.security.gateway_keys import GatewayKeyHasher


def make_runtime():
    hasher = GatewayKeyHasher(b"p" * 32)
    issued = hasher.issue(key_id="key", client_id="client")
    client = GatewayClient(
        id="client",
        name="client",
        permissions=ClientPermissions(
            protocols=frozenset({ClientProtocol.OPENAI_RESPONSES}),
            allowed_models=frozenset({"allowed-alias"}),
        ),
    )
    registry = ModelRegistry(
        [
            CanonicalModel("allowed", frozenset({"allowed-alias"}), frozenset()),
            CanonicalModel("hidden", frozenset(), frozenset()),
        ],
        [],
    )
    return (
        GatewayRuntime(
            key_store=InMemoryGatewayKeyStore([issued.record], [client]),
            key_hasher=hasher,
            model_registry=registry,
            routing_engine=RoutingEngine(registry),
            provider_states=(),
            credential_states=(),
            provider_model_adapters={},
            credentials={},
            http_client=None,
        ),
        issued.plaintext,
    )


def test_models_requires_authentication_and_filters_client_models() -> None:
    runtime, key = make_runtime()
    app = create_app(Settings(environment="test", _env_file=None), runtime)

    with TestClient(app) as client:
        unauthorized = client.get("/v1/models")
        authorized = client.get("/v1/models", headers={"authorization": f"Bearer {key}"})

    assert unauthorized.status_code == 401
    assert authorized.status_code == 200
    assert authorized.json()["data"] == [
        {"id": "allowed", "object": "model", "owned_by": "gateway"}
    ]


def test_production_is_not_ready_without_runtime_configuration() -> None:
    app = create_app(Settings(environment="production", _env_file=None))

    with TestClient(app) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}
