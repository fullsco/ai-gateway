"""Instrument asyncpg public acquire API without modifying driver internals."""

from gateway.metrics import measured


class Acquisition:
    def __init__(self, pool, timeout):
        self.pool = pool
        self.timeout = timeout
        self.connection = None

    @measured("db_pool_wait")
    async def get(self):
        if self.connection is not None:
            raise RuntimeError("Connection acquisition cannot be reused")
        self.connection = await self.pool.acquire(timeout=self.timeout)
        return self.connection

    def __await__(self):
        return self.get().__await__()

    async def __aenter__(self):
        return await self.get()

    async def __aexit__(self, *exc):
        await self.pool.release(self.connection, timeout=2)


class MeasuredPool:
    def __init__(self, pool):
        self.raw_pool = pool

    def __getattr__(self, name):
        return getattr(self.raw_pool, name)

    def acquire(self, *, timeout=5):
        return Acquisition(self.raw_pool, timeout)

    async def _query(self, method, *args, **kwargs):
        async with self.acquire() as connection:
            return await getattr(connection, method)(*args, **kwargs)

    async def fetch(self, *args, **kwargs):
        return await self._query("fetch", *args, **kwargs)

    async def fetchrow(self, *args, **kwargs):
        return await self._query("fetchrow", *args, **kwargs)

    async def fetchval(self, *args, **kwargs):
        return await self._query("fetchval", *args, **kwargs)

    async def execute(self, *args, **kwargs):
        return await self._query("execute", *args, **kwargs)

    async def executemany(self, *args, **kwargs):
        return await self._query("executemany", *args, **kwargs)
