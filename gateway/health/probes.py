import asyncio
import logging
from datetime import UTC, datetime

import httpx

from gateway.logging import log_event
from gateway.observability import PassiveHealthEvent, PassiveHealthRecorder
from gateway.providers import ErrorCategory, ProviderError
from gateway.runtime import GatewayRuntime

logger = logging.getLogger("gateway.health.probes")


async def run_health_probes(
    runtime: GatewayRuntime,
    recorder: PassiveHealthRecorder | None,
) -> None:
    if runtime.http_client is None:
        return
    for state in runtime.credential_states:
        credential = runtime.credentials.get(state.credential_id)
        if credential is None or not state.enabled:
            continue
        routes = [
            route
            for route in runtime.model_registry.list_provider_models()
            if route.provider_id == state.provider_id
            and (
                not state.supported_provider_model_ids
                or route.id in state.supported_provider_model_ids
            )
        ]
        for route in routes:
            route_id = route.id
            if not await runtime.route_controls.allow(route_id):
                continue
            adapter = runtime.provider_model_adapters.get(route_id)
            if adapter is None:
                await runtime.route_controls.abandon(route_id)
                continue
            started = datetime.now(UTC)
            error: ProviderError | None = None
            status: int | None = None
            try:
                probe = adapter.create_probe_request(credential)
                request = runtime.http_client.build_request(
                    probe.method,
                    probe.url,
                    headers=probe.headers,
                    timeout=probe.timeout,
                )
                response = await runtime.http_client.send(request)
                status = response.status_code
                if status in {401, 403} or status >= 500 or status in {408, 504}:
                    error = adapter.normalize_error(response)
                await response.aclose()
            except (TimeoutError, httpx.TransportError) as exc:
                error = ProviderError(
                    category=ErrorCategory.TIMEOUT
                    if isinstance(exc, httpx.TimeoutException)
                    else ErrorCategory.PROVIDER_UNAVAILABLE,
                    message="Health probe transport failure.",
                    retryable=True,
                )
            except Exception as exc:
                log_event(
                    logger,
                    logging.WARNING,
                    "health_probe_failed",
                    error_type=type(exc).__name__,
                )
                await runtime.route_controls.abandon(route_id)
                continue

            observed = datetime.now(UTC)
            latency_ms = round((observed - started).total_seconds() * 1000, 3)
            if error is None:
                await runtime.route_controls.record_success(route_id)
            elif error.category in {
                ErrorCategory.PROVIDER_UNAVAILABLE,
                ErrorCategory.TIMEOUT,
            }:
                await runtime.route_controls.record_failure(route_id, now=observed)
            else:
                await runtime.route_controls.abandon(route_id)
            if recorder is not None:
                recorder.submit(
                    PassiveHealthEvent(
                        provider_id=state.provider_id,
                        credential_id=state.credential_id,
                        provider_model_id=route_id,
                        request_id="health-probe",
                        attempt_number=0,
                        observed_at=observed,
                        latency_ms=latency_ms,
                        error_category=error.category.value if error else None,
                        upstream_status=error.upstream_status if error else status,
                        retry_after_seconds=error.retry_after_seconds if error else None,
                    )
                )


async def health_probe_loop(
    interval_seconds: float,
    runtime_getter,
    recorder_getter,
) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        runtime = runtime_getter()
        if runtime is not None:
            await run_health_probes(runtime, recorder_getter())
