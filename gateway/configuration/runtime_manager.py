import asyncio
import logging
from dataclasses import replace

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
        self._refresh_lock = asyncio.Lock()

    @property
    def runtime(self) -> GatewayRuntime | None:
        return self._runtime

    @property
    def version(self) -> int | None:
        return self._version

    async def refresh(self) -> GatewayRuntime | None:
        async with self._refresh_lock:
            return await self._refresh()

    async def _refresh(self) -> GatewayRuntime | None:
        snapshot = await self._cache.refresh()
        if snapshot.version == self._version:
            return self._runtime
        runtime = self._build(snapshot)
        previous = self._runtime
        if previous is not None:
            # Transport configuration is process-scoped. Snapshot changes reuse it,
            # so old streams and new requests share one bounded connection pool.
            if runtime.http_client is not None and runtime.http_client is not previous.http_client:
                await runtime.http_client.aclose()
            runtime = replace(runtime, http_client=previous.http_client,
                              route_controls=previous.route_controls)
        limits = {
            model.id: model.max_concurrency
            for model in runtime.model_registry.list_provider_models()
        }
        # Request-local dataclass replacements share the registry, but not the
        # runtime's identity. Its lifetime covers every snapshot that can dispatch.
        runtime.route_controls.retain_routes(runtime.model_registry, limits)
        runtime.route_controls.reconfigure(limits)
        self._runtime = runtime
        self._version = snapshot.version
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
        if self._runtime is not None and isinstance(self._builder, RuntimeBuilder):
            return self._builder.build(snapshot.payload, http_client=self._runtime.http_client)
        return self._builder.build(snapshot.payload)
