import asyncio
from datetime import UTC, datetime

from gateway.health.circuit_breaker import CircuitBreaker, CircuitSnapshot, CircuitState


class ConcurrencyLease:
    def __init__(self, semaphore: asyncio.Semaphore | None) -> None:
        self._semaphore = semaphore
        self._released = False

    def release(self) -> None:
        if not self._released:
            self._released = True
            if self._semaphore is not None:
                self._semaphore.release()


class RouteControls:
    def __init__(self, limits: dict[str, int] | None = None) -> None:
        self._breaker = CircuitBreaker()
        self._circuits: dict[str, CircuitSnapshot] = {}
        self._circuit_lock = asyncio.Lock()
        self._limiters = {
            route_id: asyncio.Semaphore(limit) for route_id, limit in (limits or {}).items()
        }

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
        semaphore = self._limiters.get(route_id)
        if semaphore is None:
            return ConcurrencyLease(None)
        try:
            await asyncio.wait_for(semaphore.acquire(), timeout=max(0, timeout_seconds))
        except TimeoutError:
            return None
        return ConcurrencyLease(semaphore)

    def circuit_snapshot(self, route_id: str) -> CircuitSnapshot:
        return self._circuits.get(route_id, CircuitSnapshot())
