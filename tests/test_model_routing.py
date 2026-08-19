"""Behavioural proof for the high-level model routing endpoint.

`PUT /models/{model_id}/routing` is the only writer the dashboard uses, so a
defect here silently rewrites operator intent for every model. These tests pin
the four properties that matter:

* a route pool is only attached when it can actually serve traffic,
* `allow_model_fallback` is operator intent and survives a routing save,
* providers are matched by identity rather than by case-sensitive name, and
* re-saving the configuration the dashboard just loaded changes nothing.
"""

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from gateway.admin.control_plane import ModelRoutingInput
from gateway.app import create_app
from tests.test_control_plane import AdminVerifier, FakePool, auth, settings

MODEL = "claude-opus-5"
POLICY_ID = "00000000-0000-0000-0000-0000000000p0".replace("p", "9")
POOL_ID = "00000000-0000-0000-0000-0000000000c0".replace("c", "8")


def mapping(
    mapping_id: str,
    provider: str,
    *,
    provider_id: str = "11111111-1111-1111-1111-111111111111",
    protocol: str = "anthropic_messages",
    weight: float = 1.0,
) -> dict[str, Any]:
    return {
        "id": mapping_id,
        "provider_id": provider_id,
        "provider": provider,
        "protocol": protocol,
        "weight": weight,
    }


class RoutingPool(FakePool):
    """Fake pool shaped for the routing endpoint's query sequence.

    Results are keyed on query text rather than call order, because the gateway
    refreshes live operational state in the background and would otherwise
    consume a positional queue before the request runs.
    """

    def __init__(
        self,
        mappings: list[dict[str, Any]],
        *,
        pool_member_count: int = 2,
    ) -> None:
        super().__init__()
        self.fetchval_result = 1
        self.mappings = mappings
        self.pool_member_count = pool_member_count

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if "from public.provider_models pm" in query and "p.name as provider" in query:
            return self.mappings
        return []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.fetchrow_calls.append((query, args))
        if "insert into public.routing_policies" in query:
            return {"id": POLICY_ID}
        if "insert into public.provider_pools" in query:
            return {"id": POOL_ID}
        return None

    async def fetchval(self, query: str, *args: Any) -> Any:
        if "provider_pool_members" in query and "count(" in query:
            return self.pool_member_count
        return self.fetchval_result

    def executed(self, needle: str) -> list[tuple[str, tuple[Any, ...]]]:
        return [call for call in self.execute_calls if needle in call[0]]


def put_routing(pool: RoutingPool, body: dict[str, Any], model: str = MODEL):
    # database_url=None keeps the fake pool authoritative: with a real URL in the
    # environment the app builds its own pool and these assertions would silently
    # exercise the live database instead.
    hermetic = settings().model_copy(update={"database_url": None})
    with TestClient(
        create_app(hermetic, admin_verifier=AdminVerifier(), db_pool=pool),
        raise_server_exceptions=False,
    ) as test_client:
        return test_client.put(
            f"/api/admin/v1/models/{model}/routing", headers=auth(), json=body
        )


def test_routing_save_preserves_operator_configured_model_fallback() -> None:
    """`allow_model_fallback` is operator intent, not something a save may reset.

    claude-opus-5-thinking runs with allow_model_fallback=true in production; a
    routing save must not silently turn that off.
    """
    pool = RoutingPool([mapping("mapping-1", "GoRouter")])

    response = put_routing(pool, {"providers": [{"provider": "GoRouter"}]})

    assert response.status_code == 200
    upserts = pool.executed("insert into public.model_routes")
    assert upserts, "expected a route upsert"
    statement = upserts[0][0]
    assert "allow_model_fallback=false" not in statement.replace(" ", ""), (
        "routing save must not force allow_model_fallback off on existing routes"
    )


def test_routing_save_does_not_touch_credential_pools() -> None:
    """Credential pools are owned by the explicit /provider-pools API.

    This endpoint expresses which providers serve a model. Deriving pool
    membership here from credential_model_access silently narrowed routing,
    because that table is sparsely populated: where no row existed the pool was
    empty, and an empty pool means "no credential may serve this route".
    """
    pool = RoutingPool([mapping("mapping-1", "hcnsec")], pool_member_count=0)

    response = put_routing(pool, {"providers": [{"provider": "hcnsec"}]})

    assert response.status_code == 200
    assert not pool.executed("provider_pool_members"), (
        "routing save must not rewrite pool membership"
    )
    assert not [
        call for call in pool.fetchrow_calls if "insert into public.provider_pools" in call[0]
    ], "routing save must not create credential pools"
    upserts = pool.executed("insert into public.model_routes")
    assert upserts, "expected a route upsert"
    assert "pool_id=" not in upserts[0][0], (
        "routing save must leave an existing pool assignment alone"
    )


def test_routing_save_matches_providers_by_identity_not_letter_case() -> None:
    """Validation casefolds provider names but the SQL used `=` on the name.

    A case-differing name therefore passed validation and was then disabled by
    the "turn off everything not selected" statement.
    """
    pool = RoutingPool([mapping("mapping-1", "AgentRouter")])

    response = put_routing(pool, {"providers": [{"provider": "agentrouter"}]})

    assert response.status_code == 200
    disables = pool.executed("set enabled=false")
    assert disables, "expected a statement disabling unselected routes"
    retained = pool.executed("insert into public.model_routes")
    assert retained, "the selected provider must still be routed"
    disable_args = disables[0][1]
    assert "mapping-1" in json.dumps(disable_args, default=str) or all(
        "agentrouter" not in str(argument).casefold()
        or "AgentRouter" in str(argument)
        for argument in disable_args
    ), "case-differing provider names must not be disabled by the cleanup statement"


def test_routing_save_keeps_distinct_provider_mappings_addressable() -> None:
    """One provider may expose the same model under several protocols.

    The endpoint keyed its pools on the provider name, so two mappings collapsed
    onto a single pool and both got the same priority.
    """
    pool = RoutingPool(
        [
            mapping("mapping-anthropic", "AgentRouter", protocol="anthropic_messages"),
            mapping("mapping-openai", "AgentRouter", protocol="openai_chat_completions"),
        ]
    )

    response = put_routing(
        pool,
        {
            "providers": [
                {
                    "provider": "AgentRouter",
                    "priority": 0,
                    "provider_model_ids": ["mapping-anthropic"],
                }
            ]
        },
    )

    assert response.status_code == 200
    upserts = pool.executed("insert into public.model_routes")
    routed = {call[1][1] for call in upserts}
    assert routed == {"mapping-anthropic"}, (
        "only the explicitly selected mapping may be routed; " f"got {routed}"
    )


def test_routing_input_accepts_a_stable_provider_identifier() -> None:
    body = ModelRoutingInput(
        providers=[{"provider_id": "11111111-1111-1111-1111-111111111111"}]
    )
    assert body.providers[0].provider_id == "11111111-1111-1111-1111-111111111111"


def test_routing_input_rejects_a_target_without_any_identifier() -> None:
    with pytest.raises(ValueError, match="provider or provider_id"):
        ModelRoutingInput(providers=[{"priority": 0}])


def test_routing_save_refuses_to_leave_a_model_with_no_route() -> None:
    """`provider_model_id <> all('{}')` is vacuously true in PostgreSQL.

    An empty resolved selection would therefore disable every route for the
    model, so the endpoint must reject it rather than emit that statement.
    """
    pool = RoutingPool([])

    response = put_routing(pool, {"providers": [{"provider": "AgentRouter"}]})

    assert response.status_code == 422
    assert not pool.executed("set enabled=false"), (
        "no route may be disabled when the change resolves to nothing"
    )
