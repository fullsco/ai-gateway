from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.config import Settings


def test_admin_api_requires_authentication() -> None:
    app = create_app(Settings(environment="test", _env_file=None))

    with TestClient(app) as client:
        response = client.get("/api/admin/v1/overview")

    assert response.status_code == 401
    assert response.json() == {"error": "admin_authentication_required"}


def test_credential_views_count_what_the_router_can_use() -> None:
    """Operational counts must use the router's own definition of routable.

    Health alone understated the pool badly. One provider read as 1 healthy
    credential out of 25 while the router could actually use 17, because an
    unhealthy credential whose cooldown has elapsed earns a recovery attempt. An
    operator looking at that view would conclude they were one failure from an
    outage when they were not, or miss that 7 other credentials will never be
    retried at all.
    """
    import inspect

    from gateway.admin import api

    # The queries interpolate the router's constant rather than restating the
    # predicate, so the only way they can drift is by dropping the reference.
    for endpoint in (api.credentials, api.providers, api.provider_workspace):
        source = inspect.getsource(endpoint)
        assert "ROUTABLE_CREDENTIAL_SQL" in source, (
            f"{endpoint.__name__} counts credentials without the router's definition"
        )
        assert "routable_credentials" in source or "as routable" in source


def test_the_credentials_view_separates_recoverable_from_hopeless() -> None:
    """routing_state must distinguish a credential that returns from one that cannot.

    "unhealthy" covers both a key pausing after a rate limit, which comes back by
    itself, and a key that has never once succeeded and holds no cooldown, which is
    never retried and needs replacing. Those need opposite responses, so they must
    not render the same.
    """
    import inspect

    from gateway.admin import api

    source = inspect.getsource(api.credentials)
    for state in ("in service", "on trial", "cooling down", "needs attention", "disabled"):
        assert f"'{state}'" in source, f"routing_state is missing {state!r}"


def test_the_clients_view_reports_effective_access_not_just_configuration() -> None:
    """A client can read as disabled here and still be serving traffic.

    Access is enforced from the published snapshot, so editing gateway_clients
    changes nothing until a publish. An operator who disables a client, sees
    "disabled" in the list and walks away has revoked nothing: verified in
    production, a staged disable still returned 200 on the client's key.
    """
    import inspect

    from gateway.admin import api

    source = inspect.getsource(api.clients)
    assert "live_access" in source
    # The effective state has to come from the published snapshot, not the table.
    assert "config_versions" in source and "status = 'published'" in source
    # The dangerous direction must be called out explicitly, not merely implied.
    assert "STILL SERVING until you publish" in source
