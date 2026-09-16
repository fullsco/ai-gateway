"""Atomic multi-scope PostgreSQL leases; lost renewal cancels the owning request."""

import asyncio
import logging
from contextlib import asynccontextmanager
from contextvars import ContextVar
from uuid import uuid4


class CapacityUnavailable(RuntimeError):
    pass


class CapacityFull(CapacityUnavailable):
    pass


owned_leases: ContextVar[list | None] = ContextVar("owned_capacity_leases", default=None)
lifetime_owner: ContextVar[asyncio.Task | None] = ContextVar(
    "capacity_lifetime_owner", default=None,
)
owned_probes: ContextVar[list | None] = ContextVar("owned_route_probes", default=None)
owned_work: ContextVar[list | None] = ContextVar("owned_capacity_work", default=None)


@asynccontextmanager
async def capacity_lifetime():
    leases = []
    token = owned_leases.set(leases)
    probes = []
    probe_token = owned_probes.set(probes)
    owner_token = lifetime_owner.set(asyncio.current_task())
    work = []
    work_token = owned_work.set(work)
    try:
        yield
    finally:
        owned_leases.reset(token)
        owned_probes.reset(probe_token)
        lifetime_owner.reset(owner_token)
        owned_work.reset(work_token)
        try:
            for pulse, _ in probes:
                pulse.cancel()
            for _, _, pulse in leases:
                pulse.cancel()
            await asyncio.gather(*(pulse for _, _, pulse in leases),
                                 *(pulse for pulse, _ in probes), return_exceptions=True)
            for _, release in probes:
                try:
                    async with asyncio.timeout(2):
                        await release()
                except Exception:
                    pass
            for manager, owner, _ in leases:
                try:
                    async with asyncio.timeout(2):
                        await manager.release(owner)
                except Exception:
                    pass
        finally:
            # Tasks copy ContextVars, retaining these same lists even after reset.
            # Break list -> task -> context -> list cycles on every exit path.
            leases.clear()
            probes.clear()
            work.clear()


async def reserve_route_capacity(pool, route, settings):
    leases = owned_leases.get()
    if leases is None:
        raise CapacityUnavailable("Distributed capacity requires an owned request lifetime")
    scopes = {
        f"provider:{route.provider_model.provider_id}": settings.distributed_provider_capacity,
        f"credential:{route.credential.credential_id}": settings.distributed_credential_capacity,
        f"route:{route.provider_model.id}": route.provider_model.max_concurrency,
    }
    from gateway.workflow_context import active_workflow

    if active_workflow.get() is not None:
        # Workflow traffic may consume only the spare share. A cap of one reserves
        # the sole interactive slot entirely; background work must wait for capacity policy.
        for scope, capacity in list(scopes.items()):
            if capacity < 2:
                raise CapacityFull("No background share is configured for this interactive scope")
            scopes[f"workflow-share:{scope}"] = max(1, capacity // 2)
    manager = DistributedCapacity(pool)
    # Retries on the same route reuse existing request-owned reservations.
    if any(getattr(existing, "scopes", None) == scopes for existing, _, _ in leases):
        return
    # Attempts are sequential: the previous upstream has been closed before this call.
    # Holding its provider scope while acquiring the next credential can self-deadlock
    # at a global capacity of one. Release previous attempt ownership before retrying.
    for existing, previous_owner, pulse in leases:
        pulse.cancel()
        await asyncio.gather(pulse, return_exceptions=True)
        await existing.release(previous_owner)
    leases.clear()
    manager.scopes = scopes
    owner = await manager.acquire(scopes)
    leases.append((manager, owner, manager.guard(owner, len(scopes))))


async def verify_route_ownership():
    # Check at dispatch, not only on the heartbeat timer. An event-loop pause may
    # have allowed a lease to expire while its cancellation callback was delayed.
    leases = owned_leases.get()
    if not leases:
        raise CapacityUnavailable("Missing distributed route ownership")
    for manager, owner, _ in leases:
        await manager.renew(owner, len(manager.scopes))


class DistributedCapacity:
    def __init__(self, pool, ttl=30):
        if not 3 <= ttl <= 300:
            raise ValueError("Capacity lease TTL must be 3–300 seconds")
        self.pool, self.ttl = pool, ttl

    async def acquire(self, scopes):
        if not scopes or len(scopes) > 8:
            raise ValueError("One to eight capacity scopes required")
        if any(not isinstance(k, str) or not 1 <= len(k) <= 200 or type(v) is not int or v < 1
               for k, v in scopes.items()):
            raise ValueError("Invalid capacity scope")
        owner = uuid4()
        try:
            async with self.pool.acquire() as connection, connection.transaction():
                effective_scopes = {}
                for scope, capacity in sorted(scopes.items()):
                    await connection.execute(
                        "insert into public.capacity_scopes(scope,capacity) values($1,$2) "
                        "on conflict do nothing", scope, capacity,
                    )
                    stored = await connection.fetchval(
                        "select capacity from public.capacity_scopes where scope=$1 for update",
                        scope,
                    )
                    # Replicas cannot independently raise global configuration.
                    effective = min(capacity, stored)
                    if scope.startswith("workflow-share:"):
                        parent = scope.removeprefix("workflow-share:")
                        parent_capacity = effective_scopes.get(parent)
                        if parent_capacity is None or parent_capacity < 2:
                            raise CapacityFull("No background share is available")
                        effective = min(effective, parent_capacity // 2)
                    effective_scopes[scope] = effective
                # Keep per-scope creation/locking in the same order as older replicas.
                # Once all parent rows are locked, prune/count/insert in batches. Counts
                # use a separate statement so concurrent commits are visible after locks.
                names = list(effective_scopes)
                await connection.execute(
                    "delete from public.capacity_leases where scope=any($1::text[]) "
                    "and expires_at<=clock_timestamp()", names,
                )
                counts = await connection.fetch(
                    "select scope,count(*) as used from public.capacity_leases "
                    "where scope=any($1::text[]) group by scope", names,
                )
                for row in counts:
                    if row["used"] >= effective_scopes[row["scope"]]:
                        raise CapacityFull("Distributed concurrency capacity is full")
                # One statement starts every scope's TTL together after all lock waits.
                await connection.execute(
                    "insert into public.capacity_leases(owner,scope,expires_at) "
                    "select $1,scope,statement_timestamp()+$3*interval '1 second' "
                    "from unnest($2::text[]) as scopes(scope)", owner, names, self.ttl,
                )
        except CapacityFull:
            raise
        except Exception as exc:
            raise CapacityUnavailable("Distributed capacity is unavailable") from exc
        return owner

    async def renew(self, owner, scope_count):
        async with self.pool.acquire() as connection, connection.transaction():
            await connection.fetch(
                "select scope from public.capacity_leases where owner=$1 order by scope for update",
                owner,
            )
            rows = await connection.fetch(
                "update public.capacity_leases set expires_at=clock_timestamp()"
                "+$2*interval '1 second' "
                "where owner=$1 and expires_at>clock_timestamp() returning scope", owner, self.ttl,
            )
            if len(rows) != scope_count:
                raise CapacityUnavailable("Distributed capacity ownership expired")

    async def release(self, owner):
        await self.pool.execute("delete from public.capacity_leases where owner=$1", owner)

    async def configure(self, scope, capacity, expected_version):
        if not isinstance(scope, str) or not 1 <= len(scope) <= 200:
            raise ValueError("Invalid capacity scope")
        if type(capacity) is not int or not 1 <= capacity <= 10000:
            raise ValueError("Invalid capacity")
        row = await self.pool.fetchrow(
            "update public.capacity_scopes set capacity=$2,version=version+1,updated_at=now() "
            "where scope=$1 and version=$3 returning scope,capacity,version",
            scope, capacity, expected_version,
        )
        if row is None:
            raise CapacityUnavailable("Capacity policy version changed or scope is missing")
        return dict(row)

    def guard(self, owner, scope_count, task=None):
        request_task = task or lifetime_owner.get() or asyncio.current_task()
        work = owned_work.get()

        async def renew_loop():
            try:
                while True:
                    await asyncio.sleep(self.ttl / 3)
                    async with asyncio.timeout(self.ttl / 3):
                        await self.renew(owner, scope_count)
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.getLogger("gateway.capacity").warning(
                    "capacity_renewal_failed active_work=%s", len(work or ())
                )
                if work:
                    for active in tuple(work):
                        if not active.done():
                            active.cancel()
                request_task.cancel()

        return asyncio.create_task(renew_loop())
