"""Cross-replica route health with one owned half-open trial."""

import asyncio
import logging
from contextvars import ContextVar
from uuid import uuid4

from gateway.distributed_capacity import CapacityUnavailable, lifetime_owner, owned_probes

trial_owners: ContextVar[dict | None] = ContextVar("route_trial_owners", default=None)


class SharedRouteControls:
    def __init__(self, pool, local):
        self.pool, self.local = pool, local
        self.pulses = {}

    async def acquire(self, *args, **kwargs):
        return await self.local.acquire(*args, **kwargs)

    async def allow(self, route_id, *, now=None):
        owners = trial_owners.get()
        if owners is None:
            owners = {}
            trial_owners.set(owners)
        owner = uuid4()
        try:
            async with self.pool.acquire() as connection, connection.transaction():
                query = (
                    "select *,clock_timestamp() as current_time from public.route_circuits "
                    "where route_id=$1 for update"
                )
                row = await connection.fetchrow(query, route_id)
                if row is None:
                    await connection.execute(
                        "insert into public.route_circuits(route_id) values($1) "
                        "on conflict do nothing", route_id,
                    )
                    row = await connection.fetchrow(query, route_id)
                clock = row["current_time"]
                if row["state"] == "closed":
                    owners[route_id] = (None, row["generation"])
                    return True
                if row["retry_at"] and row["retry_at"] > clock:
                    return False
                if row["probe_owner"] and row["probe_expires_at"] > clock:
                    return False
                await connection.execute(
                    "update public.route_circuits set state='half_open',probe_owner=$2,"
                    "probe_expires_at=clock_timestamp()+interval '15 minutes',updated_at=now() "
                    "where route_id=$1", route_id, owner,
                )
                owners[route_id] = (owner, row["generation"])
                probes = owned_probes.get()
                if probes is not None:
                    task = lifetime_owner.get() or asyncio.current_task()

                    async def renew():
                        try:
                            while True:
                                await asyncio.sleep(30)
                                async with asyncio.timeout(10):
                                    changed = await self.pool.fetchval(
                                        "update public.route_circuits set probe_expires_at="
                                        "clock_timestamp()+interval '15 minutes' "
                                        "where route_id=$1 and probe_owner=$2 and generation=$3 "
                                        "and state='half_open' "
                                        "and probe_expires_at>clock_timestamp() "
                                        "returning route_id", route_id, owner, row["generation"],
                                    )
                                    if changed is None:
                                        raise CapacityUnavailable("Shared probe ownership lost")
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            task.cancel()

                    async def release():
                        await self.pool.execute(
                            "update public.route_circuits set probe_owner=null,"
                            "probe_expires_at=null "
                            "where route_id=$1 and probe_owner=$2", route_id, owner,
                        )

                    pulse = asyncio.create_task(renew())
                    self.pulses[owner] = pulse
                    probes.append((pulse, release))
                return True
        except Exception as exc:
            raise CapacityUnavailable("Shared route circuit is unavailable") from exc

    async def outcome(self, route_id, outcome):
        owners = trial_owners.get() or {}
        admission = owners.pop(route_id, None)
        if admission is None:
            return
        owner, generation = admission
        if owner in self.pulses:
            pulse = self.pulses.pop(owner)
            pulse.cancel()
            await asyncio.gather(pulse, return_exceptions=True)
        async with self.pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                "select *,clock_timestamp() as current_time from public.route_circuits "
                "where route_id=$1 for update", route_id,
            )
            if row is None:
                return
            if row["generation"] != generation:
                return
            if row["state"] != "closed" and row["probe_owner"] != owner:
                return  # Late completions cannot reset a newer probe owner's decision.
            if (owner is not None and row["probe_expires_at"] is not None
                    and row["probe_expires_at"] <= row["current_time"]):
                return
            if outcome == "abandon":
                if owner is not None:
                    await connection.execute(
                        "update public.route_circuits set probe_owner=null,probe_expires_at=null "
                        "where route_id=$1 and probe_owner=$2", route_id, owner,
                    )
                return
            failures = row["failures"] + 1 if outcome == "failure" else 0
            successes = row["successes"] + 1 if outcome == "success" else 0
            state = row["state"]
            if outcome == "failure" and (state == "half_open" or failures >= 3):
                state = "open"
            elif outcome == "success" and (state == "closed" or successes >= 2):
                state, successes = "closed", 0
            await connection.execute(
                "update public.route_circuits set state=$2,failures=$3,successes=$4,"
                "retry_at=case when $2='open' then now()+interval '30 seconds' else null end,"
                "generation=generation+case when $2<>state then 1 else 0 end,"
                "probe_owner=null,probe_expires_at=null,updated_at=now() where route_id=$1",
                route_id, state, failures, successes,
            )

    async def record_success(self, route_id):
        await self._record(route_id, "success")

    async def record_failure(self, route_id, *, now=None):
        await self._record(route_id, "failure")

    async def abandon(self, route_id):
        await self._record(route_id, "abandon")

    async def _record(self, route_id, outcome):
        try:
            await self.outcome(route_id, outcome)
        except Exception:
            # A failed health write must not cause replay after a successful provider call.
            logging.getLogger("gateway.routing").error("shared_circuit_outcome_unavailable")
