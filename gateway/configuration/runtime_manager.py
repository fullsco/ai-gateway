import logging

from gateway.configuration.runtime_builder import RuntimeBuilder
from gateway.configuration.snapshots import CachedConfiguration, ConfigSnapshot
from gateway.logging import log_event
from gateway.runtime import GatewayRuntime

logger = logging.getLogger("gateway.configuration")


class RuntimeManager:
    def __init__(self, cache: CachedConfiguration, builder: RuntimeBuilder) -> None:
        self._cache = cache
        self._builder = builder
        self._runtime: GatewayRuntime | None = None
        self._version: int | None = None
        self._retired_runtimes: list[GatewayRuntime] = []

    @property
    def runtime(self) -> GatewayRuntime | None:
        return self._runtime

    @property
    def version(self) -> int | None:
        return self._version

    async def refresh(self) -> GatewayRuntime | None:
        snapshot = await self._cache.refresh()
        if snapshot.version == self._version:
            return self._runtime
        runtime = self._build(snapshot)
        previous = self._runtime
        self._runtime = runtime
        self._version = snapshot.version
        if previous is not None:
            self._retired_runtimes.append(previous)
        log_event(logger, logging.INFO, "runtime_configured", version=snapshot.version)
        return runtime

    async def close(self) -> None:
        runtimes = [runtime for runtime in [self._runtime, *self._retired_runtimes] if runtime]
        for runtime in runtimes:
            if runtime.http_client is not None:
                await runtime.http_client.aclose()
        self._runtime = None
        self._retired_runtimes.clear()

    def _build(self, snapshot: ConfigSnapshot) -> GatewayRuntime:
        return self._builder.build(snapshot.payload)
