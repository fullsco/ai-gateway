"""Bounded, process-local readiness checks for required database dependencies."""

import asyncio
from time import monotonic

from gateway.config import Settings

PROBE_TIMEOUT_SECONDS = 0.75
CACHE_SECONDS = 0.5


def requires_database(settings: Settings) -> bool:
    return (
        settings.distributed_admission_enabled
        or settings.durable_accounting_enabled
        or settings.workflows_enabled
        or settings.service_role == "workflow"
    )


class DependencyHealth:
    """Share one probe and cache both success and failure across readiness callers."""

    def __init__(self) -> None:
        self._pool = None
        self._probe: asyncio.Task | None = None
        self._query: asyncio.Task | None = None
        self._healthy = False
        self._expires = 0.0

    async def healthy(self, pool) -> bool:
        if pool is None:
            return False
        if self._probe is not None and not self._probe.done():
            if pool is not self._pool:
                return False
            # A disconnected health-check client must not cancel everybody's probe.
            return await asyncio.shield(self._probe)
        if pool is self._pool and monotonic() < self._expires:
            return self._healthy
        if self._query is not None and not self._query.done():
            # Pool acquisition/release can take time to honor cancellation. Do not
            # queue additional queries, or accept a late success after the deadline.
            return False
        self._pool = pool
        self._probe = asyncio.create_task(self._refresh(pool))
        self._probe.add_done_callback(self._consume_exception)
        return await asyncio.shield(self._probe)

    async def _refresh(self, pool) -> bool:
        query = asyncio.create_task(self._select_one(pool))
        self._query = query
        query.add_done_callback(self._consume_exception)
        try:
            # wait_for waits for cancellation cleanup and can exceed its timeout.
            # This bounds the entire pool operation, including acquire and release.
            done, _ = await asyncio.wait({query}, timeout=PROBE_TIMEOUT_SECONDS)
            self._healthy = bool(done) and not query.cancelled() and query.result()
            self._expires = monotonic() + CACHE_SECONDS
            return self._healthy
        finally:
            if not query.done():
                query.cancel()

    @staticmethod
    async def _select_one(pool) -> bool:
        try:
            value = await pool.fetchval("SELECT 1", timeout=PROBE_TIMEOUT_SECONDS)
            # A truthy placeholder (including an unconfigured AsyncMock) is not
            # evidence of a working DB. asyncpg returns an int for this query.
            return type(value) is int and value == 1
        except Exception:
            return False

    @staticmethod
    def _consume_exception(task: asyncio.Task) -> None:
        if not task.cancelled():
            task.exception()

    async def close(self) -> None:
        tasks = {task for task in (self._probe, self._query)
                 if task is not None and not task.done()}
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=0.1)
        self._healthy = False
        self._expires = 0.0
