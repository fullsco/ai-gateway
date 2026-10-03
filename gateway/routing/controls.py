import asyncio
import weakref
from collections.abc import Callable, Iterable
from datetime import UTC, datetime

from gateway.health.circuit_breaker import CircuitBreaker, CircuitSnapshot, CircuitState


class ConcurrencyLease:
    def __init__(self, semaphore: asyncio.Semaphore | None,
                 on_release: Callable[[], None] | None = None) -> None:
        self._semaphore = semaphore
        self._released = False
        self._on_release = on_release

    def release(self) -> None:
        if not self._released:
            self._released = True
            if self._semaphore is not None:
                self._semaphore.release()
            if self._on_release is not None:
                self._on_release()


class RouteControls:
    def __init__(self, limits: dict[str, int] | None = None) -> None:
        self._breaker = CircuitBreaker()
        self._circuits: dict[str, CircuitSnapshot] = {}
        self._circuit_lock = asyncio.Lock()
        self._limits = dict(limits or {})
        self._current_routes = set(self._limits)
        self._active: dict[str, int] = {}
        self._changed = asyncio.Event()
        self._owners: dict[int, tuple[weakref.ReferenceType, frozenset[str]]] = {}
        self._route_owners: dict[str, int] = {}

    def retain_routes(self, owner: object, route_ids: Iterable[str]) -> None:
        """Keep state until a snapshot registry or an acquisition/lease is gone."""
        key = id(owner)
        if key in self._owners:
            return
        routes = frozenset(route_ids)
        reference = weakref.ref(owner, lambda _: self._release_owner(key))
        self._owners[key] = (reference, routes)
        for route_id in routes:
            self._route_owners[route_id] = self._route_owners.get(route_id, 0) + 1

    def _release_owner(self, key: int) -> None:
        _, routes = self._owners.pop(key)
        for route_id in routes:
            remaining = self._route_owners[route_id] - 1
            if remaining:
                self._route_owners[route_id] = remaining
            else:
                del self._route_owners[route_id]
        self._prune(routes)

    def _prune(self, route_ids: Iterable[str]) -> None:
        for route_id in route_ids:
            if (route_id not in self._current_routes
                    and route_id not in self._route_owners
                    and not self._active.get(route_id, 0)):
                self._limits.pop(route_id, None)
                self._circuits.pop(route_id, None)

    def reconfigure(self, limits: dict[str, int]) -> None:
        # Existing routes keep their active counts and circuit history. Removed
        # routes retain their last limit until every snapshot and lease is gone.
        self._current_routes = set(limits)
        self._limits.update(limits)
        self._prune(self._limits.keys() | self._circuits.keys())
        self._changed.set()

    async def allow(self, route_id: str, *, now: datetime | None = None) -> bool:
        async with self._circuit_lock:
            snapshot = self._circuits.get(route_id, CircuitSnapshot())
            admitted = self._breaker.can_attempt(snapshot, now=now or datetime.now(UTC))
            if admitted is None:
                return False
            self._circuits[route_id] = admitted
            return True

    async def record_success(self, route_id: str) -> None:
        async with self._circuit_lock:
            snapshot = self._circuits.get(route_id, CircuitSnapshot())
            self._circuits[route_id] = self._breaker.record_success(snapshot)

    async def record_failure(self, route_id: str, *, now: datetime | None = None) -> None:
        async with self._circuit_lock:
            snapshot = self._circuits.get(route_id, CircuitSnapshot())
            self._circuits[route_id] = self._breaker.record_failure(
                snapshot, now=now or datetime.now(UTC)
            )

    async def abandon(self, route_id: str) -> None:
        async with self._circuit_lock:
            snapshot = self._circuits.get(route_id, CircuitSnapshot())
            if snapshot.state is CircuitState.HALF_OPEN and snapshot.probe_in_flight:
                self._circuits[route_id] = CircuitSnapshot(
                    state=CircuitState.HALF_OPEN,
                    consecutive_failures=snapshot.consecutive_failures,
                    consecutive_successes=snapshot.consecutive_successes,
                    opened_at=snapshot.opened_at,
                )

    async def acquire(self, route_id: str, *, timeout_seconds: float) -> ConcurrencyLease | None:
        if route_id not in self._limits:
            return ConcurrencyLease(None)

        async def acquire_slot():
            while self._active.get(route_id, 0) >= self._limits[route_id]:
                self._changed.clear()
                await self._changed.wait()
            self._active[route_id] = self._active.get(route_id, 0) + 1

        def release():
            remaining = self._active[route_id] - 1
            if remaining:
                self._active[route_id] = remaining
            else:
                del self._active[route_id]
            self._changed.set()

        lease = ConcurrencyLease(None, release)
        # Pin before waiting, and through the lease's lifetime rather than just
        # release(): stream finalization records its circuit outcome after release.
        self.retain_routes(lease, (route_id,))
        try:
            await asyncio.wait_for(acquire_slot(), timeout=max(0, timeout_seconds))
        except TimeoutError:
            self._release_owner(id(lease))
            return None
        except BaseException:
            self._release_owner(id(lease))
            raise
        return lease

    def circuit_snapshot(self, route_id: str) -> CircuitSnapshot:
        return self._circuits.get(route_id, CircuitSnapshot())
