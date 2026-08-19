"""Real-PostgreSQL proof that a routing save preserves operator intent.

The fake-pool tests pin the SQL the endpoint emits. These pin what PostgreSQL
actually does with it, which is where the damaging defects lived: an empty
provider pool silently means "no credential may serve this route", and a
case-differing provider name silently missed the cleanup statement.

Every test runs inside a transaction that is always rolled back, so the live
configuration is never modified.
"""

import json
import os
from typing import Any

import pytest

from gateway.admin.control_plane import ModelRoutingInput, model_routing, update_model_routing
from gateway.configuration import create_pool

pytestmark = pytest.mark.integration


def _database_url() -> str:
    value = os.environ.get("GATEWAY_DATABASE_URL")
    if not value:
        pytest.skip("GATEWAY_DATABASE_URL is required for PostgreSQL integration tests")
    return value


class _Verifier:
    def __init__(self, subject: str) -> None:
        self._subject = subject

    async def verify(self, token: str) -> Any:
        from gateway.admin.auth import AdminClaims

        return AdminClaims(subject=self._subject, email=None, role="admin")


class _State:
    def __init__(self, **values: Any) -> None:
        self.__dict__.update(values)


class _App:
    def __init__(self, state: _State) -> None:
        self.state = state


class _Request:
    """Minimal Request stand-in carrying an explicit control-plane connection."""

    def __init__(self, connection: Any, actor: str) -> None:
        self.app = _App(_State(admin_verifier=_Verifier(actor), db_pool=None))
        self.state = _State(control_plane_connection=connection)
        self.headers = {"authorization": "Bearer admin-session"}


async def _actor(connection: Any) -> str:
    """audit_logs.actor_id is a real FK, so the audit row needs a real user."""
    actor = await connection.fetchval("select id::text from auth.users limit 1")
    if actor is None:
        pytest.skip("no auth.users row available to attribute the audit entry to")
    return str(actor)


async def _routing_state(connection: Any, model_id: str) -> list[dict[str, Any]]:
    """The routing facts an operator cares about, in a comparable form."""
    rows = await connection.fetch(
        """
        select p.name as provider, pm.id::text as mapping, pm.protocol,
               r.enabled, r.priority, r.allow_model_fallback,
               r.pool_id::text as pool_id,
               (coalesce(r.enabled,false) and p.enabled and pm.enabled) as active,
               (select count(*) from public.provider_pool_members ppm
                 where ppm.pool_id=r.pool_id) as pool_members
        from public.provider_models pm
        join public.providers p on p.id=pm.provider_id
        left join public.model_routes r
          on r.provider_model_id=pm.id and r.model_id=pm.model_id
        where pm.model_id=$1
        order by p.name, pm.protocol
        """,
        model_id,
    )
    return [dict(row) for row in rows]


async def _put(connection: Any, model_id: str, body: dict[str, Any]) -> Any:
    return await update_model_routing(
        model_id, _Request(connection, await _actor(connection)), ModelRoutingInput(**body)
    )


async def _models_with_routes(connection: Any) -> list[str]:
    rows = await connection.fetch(
        "select distinct model_id from public.model_routes where enabled order by model_id"
    )
    return [str(row["model_id"]) for row in rows]


@pytest.mark.asyncio
async def test_resaving_the_loaded_configuration_changes_nothing() -> None:
    """Opening the models workspace and pressing save must not alter routing.

    This is the property the dashboard depends on: the payload it builds from a
    GET must round-trip without changing which provider serves traffic, at what
    priority, under which credential restriction.

    The endpoint is declarative, so a route that is already inert (its provider
    or mapping is disabled, so it carries no traffic) may have its stored
    `enabled` flag normalised to false. That is compared separately below,
    because it cannot change behaviour, whereas everything in `effective` can.
    """

    def effective(rows: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
        return [
            (
                row["provider"],
                row["mapping"],
                row["active"],
                row["priority"] if row["active"] else None,
                row["pool_id"],
                row["allow_model_fallback"],
            )
            for row in rows
        ]

    pool = await create_pool(_database_url())
    try:
        async with pool.acquire() as connection:
            transaction = connection.transaction()
            await transaction.start()
            try:
                checked = 0
                for model_id in await _models_with_routes(connection):
                    before = await _routing_state(connection, model_id)
                    routed = [row for row in before if row["active"]]
                    if not routed:
                        continue
                    payload = {
                        "providers": [
                            {
                                "provider": row["provider"],
                                "priority": row["priority"],
                                "provider_model_ids": [row["mapping"]],
                                "fallback": index > 0,
                            }
                            for index, row in enumerate(
                                sorted(routed, key=lambda item: item["priority"])
                            )
                        ]
                    }
                    response = await _put(connection, model_id, payload)
                    assert response.status_code == 200, (
                        f"{model_id}: {response.body!r}"
                    )
                    after = await _routing_state(connection, model_id)
                    assert effective(after) == effective(before), (
                        f"re-saving {model_id} changed effective routing:\n"
                        f"before={effective(before)}\nafter ={effective(after)}"
                    )
                    inert_before = {
                        row["mapping"] for row in before if not row["active"]
                    }
                    changed = {
                        row["mapping"]
                        for row in after
                        if row
                        not in before
                        and row["mapping"] not in inert_before
                    }
                    assert not changed, (
                        f"{model_id}: a route that was carrying traffic changed: {changed}"
                    )
                    checked += 1
                assert checked, "no model had an active route to verify"
            finally:
                await transaction.rollback()
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_routing_save_never_strands_a_model_behind_an_empty_pool() -> None:
    """A save must not leave a route pointing at a pool that cannot serve it."""
    pool = await create_pool(_database_url())
    try:
        async with pool.acquire() as connection:
            transaction = connection.transaction()
            await transaction.start()
            try:
                for model_id in await _models_with_routes(connection):
                    routed = [
                        row
                        for row in await _routing_state(connection, model_id)
                        if row["active"]
                    ]
                    if not routed:
                        continue
                    response = await _put(
                        connection,
                        model_id,
                        {
                            "providers": [
                                {"provider": row["provider"], "priority": row["priority"]}
                                for row in routed
                            ]
                        },
                    )
                    assert response.status_code == 200
                    for row in await _routing_state(connection, model_id):
                        if not row["active"] or row["pool_id"] is None:
                            continue
                        assert row["pool_members"] > 0, (
                            f"{model_id} via {row['provider']} points at an empty pool, "
                            "which excludes every credential"
                        )
            finally:
                await transaction.rollback()
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_routing_save_preserves_model_fallback_on_real_routes() -> None:
    """allow_model_fallback is operator intent and must survive a save."""
    pool = await create_pool(_database_url())
    try:
        async with pool.acquire() as connection:
            transaction = connection.transaction()
            await transaction.start()
            try:
                target = await connection.fetchrow(
                    """
                    select r.model_id, p.name as provider, r.priority
                    from public.model_routes r
                    join public.provider_models pm on pm.id=r.provider_model_id
                    join public.providers p on p.id=pm.provider_id
                    where r.allow_model_fallback and r.enabled
                    limit 1
                    """
                )
                if target is None:
                    # Establish the condition rather than skip: the guarantee
                    # must hold regardless of current data.
                    target = await connection.fetchrow(
                        """
                        update public.model_routes r set allow_model_fallback=true
                        where r.id = (
                          select r2.id from public.model_routes r2 where r2.enabled limit 1
                        )
                        returning r.model_id, r.priority,
                          (select p.name from public.providers p
                            join public.provider_models pm on pm.provider_id=p.id
                           where pm.id=r.provider_model_id) as provider
                        """
                    )
                assert target is not None, "no enabled route available to test"

                response = await _put(
                    connection,
                    target["model_id"],
                    {
                        "providers": [
                            {
                                "provider": target["provider"],
                                "priority": target["priority"],
                            }
                        ]
                    },
                )
                assert response.status_code == 200

                preserved = await connection.fetchval(
                    """
                    select bool_or(r.allow_model_fallback)
                    from public.model_routes r
                    join public.provider_models pm on pm.id=r.provider_model_id
                    join public.providers p on p.id=pm.provider_id
                    where r.model_id=$1 and p.name=$2
                    """,
                    target["model_id"],
                    target["provider"],
                )
                assert preserved is True, (
                    "routing save reset allow_model_fallback on an existing route"
                )
            finally:
                await transaction.rollback()
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_case_differing_provider_name_still_resolves_and_stays_routed() -> None:
    """Validation casefolded names while the SQL compared them with `=`."""
    pool = await create_pool(_database_url())
    try:
        async with pool.acquire() as connection:
            transaction = connection.transaction()
            await transaction.start()
            try:
                target = await connection.fetchrow(
                    """
                    select r.model_id, p.name as provider, r.priority
                    from public.model_routes r
                    join public.provider_models pm on pm.id=r.provider_model_id
                    join public.providers p on p.id=pm.provider_id
                    where r.enabled and p.enabled and pm.enabled
                      and p.name <> lower(p.name)
                    limit 1
                    """
                )
                if target is None:
                    pytest.skip("no mixed-case provider name available")

                response = await _put(
                    connection,
                    target["model_id"],
                    {
                        "providers": [
                            {
                                "provider": str(target["provider"]).lower(),
                                "priority": target["priority"],
                            }
                        ]
                    },
                )
                assert response.status_code == 200, response.body

                still_routed = await connection.fetchval(
                    """
                    select bool_or(r.enabled)
                    from public.model_routes r
                    join public.provider_models pm on pm.id=r.provider_model_id
                    join public.providers p on p.id=pm.provider_id
                    where r.model_id=$1 and p.name=$2
                    """,
                    target["model_id"],
                    target["provider"],
                )
                assert still_routed is True, (
                    "a lowercase provider name disabled the route it selected"
                )
            finally:
                await transaction.rollback()
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_get_routing_payload_round_trips_through_the_put_contract() -> None:
    """Whatever GET reports as routed must be accepted verbatim by PUT."""
    pool = await create_pool(_database_url())
    try:
        async with pool.acquire() as connection:
            transaction = connection.transaction()
            await transaction.start()
            try:
                for model_id in await _models_with_routes(connection):
                    request = _Request(connection, await _actor(connection))
                    read = await model_routing(model_id, request)
                    assert read.status_code == 200
                    rows = json.loads(read.body)["data"]
                    routed = [row for row in rows if row.get("route_active") is True]
                    if not routed:
                        continue
                    response = await _put(
                        connection,
                        model_id,
                        {
                            "providers": [
                                {
                                    "provider": row["provider"],
                                    "priority": row.get("priority") or 100,
                                    "provider_model_ids": [row["provider_model_id"]],
                                }
                                for row in routed
                            ]
                        },
                    )
                    assert response.status_code == 200, (
                        f"{model_id} GET payload rejected by PUT: {response.body!r}"
                    )
            finally:
                await transaction.rollback()
    finally:
        await pool.close()
