import asyncio
import logging
from datetime import UTC, datetime

import httpx

from gateway.health.limits import HealthProbeLimiter
from gateway.logging import log_event
from gateway.observability import PassiveHealthEvent, PassiveHealthRecorder
from gateway.providers import ErrorCategory, ProviderError
from gateway.runtime import GatewayRuntime

logger = logging.getLogger("gateway.health.probes")


async def run_health_probes(
    runtime: GatewayRuntime,
    recorder: PassiveHealthRecorder | None,
    limiter: HealthProbeLimiter | None = None,
    *,
    provider_id: str | None = None,
    credential_id: str | None = None,
    manual: bool = False,
) -> dict[str, int]:
    summary = {"contacted": 0, "skipped": 0, "credentials_without_route": 0}
    if runtime.http_client is None:
        return summary
    if limiter is None and not manual:
        summary["skipped"] = len(runtime.credential_states)
        log_event(logger, logging.WARNING, "health_probe_limiter_unavailable")
        return summary
    for state in runtime.credential_states:
        if provider_id is not None and state.provider_id != provider_id:
            continue
        if credential_id is not None and state.credential_id != credential_id:
            continue
        credential = runtime.credentials.get(state.credential_id)
        if credential is None or not state.enabled:
            continue
        routes = sorted(
            [
            route
            for route in runtime.model_registry.list_provider_models()
            if route.provider_id == state.provider_id
            and (
                not state.supported_provider_model_ids
                or route.id in state.supported_provider_model_ids
            )
            and (
                route.allowed_credential_ids is None
                or state.credential_id in route.allowed_credential_ids
            )
            ],
            key=lambda route: (route.priority, route.id),
        )
        if not routes:
            summary["credentials_without_route"] += 1
            continue
        selected = routes[
            limiter.route_index(state.credential_id, len(routes)) if limiter else 0
        ]
        for route in [selected]:
            route_id = route.id
            reservation_token = "manual-local"
            if limiter is not None:
                try:
                    reservation = await limiter.reserve(
                    state.provider_id,
                    state.credential_id,
                    route_id,
                    manual=manual,
                    )
                except Exception as exc:
                    summary["skipped"] += 1
                    log_event(
                        logger,
                        logging.WARNING,
                        "health_probe_reservation_failed",
                        credential_id=state.credential_id,
                        error_type=type(exc).__name__,
                    )
                    continue
                if not reservation.startswith("reserved:"):
                    summary["skipped"] += 1
                    continue
                reservation_token = reservation.partition(":")[2]
            adapter = runtime.provider_model_adapters.get(route_id)
            if adapter is None:
                if limiter is not None:
                    await _complete_probe(
                        limiter,
                        state.credential_id,
                        reservation_token,
                        success=False,
                        result="adapter_unavailable",
                    )
                continue
            started = datetime.now(UTC)
            error: ProviderError | None = None
            status: int | None = None
            response: httpx.Response | None = None
            try:
                probe = adapter.create_probe_request(
                    credential,
                    model=route.upstream_model_id,
                )
                request = runtime.http_client.build_request(
                    probe.method,
                    probe.url,
                    headers=probe.headers,
                    json=probe.json_body,
                    timeout=probe.timeout,
                )
                summary["contacted"] += 1
                response = await runtime.http_client.send(request)
                status = response.status_code
                if status >= 400:
                    error = adapter.normalize_error(response)
                    # A synthetic probe cannot distinguish a provider's WAF
                    # challenge from credential rejection on HTTP 403. Keep
                    # real request classification strict, but do not poison
                    # credential eligibility from an ambiguous probe.
                    if (
                        status == 403
                        and error.category is ErrorCategory.UPSTREAM_AUTHENTICATION_ERROR
                    ):
                        error = ProviderError(
                            category=ErrorCategory.UPSTREAM_WAF_REJECTION,
                            message="Health probe received an ambiguous upstream 403.",
                            retryable=True,
                            upstream_status=status,
                        )
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
                if limiter is not None:
                    await _complete_probe(
                        limiter,
                        state.credential_id,
                        reservation_token,
                        success=False,
                        result=f"internal_error:{type(exc).__name__}",
                    )
                continue
            finally:
                if response is not None:
                    await response.aclose()

            observed = datetime.now(UTC)
            latency_ms = round((observed - started).total_seconds() * 1000, 3)
            if limiter is not None:
                await _complete_probe(
                    limiter,
                    state.credential_id,
                    reservation_token,
                    success=error is None,
                    result=error.category.value if error else "healthy",
                )
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
                        source="manual" if manual else "automatic",
                    )
                )
    return summary


async def _complete_probe(
    limiter: HealthProbeLimiter,
    credential_id: str,
    reservation_token: str,
    *,
    success: bool,
    result: str,
) -> None:
    try:
        await limiter.complete(
            credential_id,
            reservation_token,
            success=success,
            result=result,
        )
    except Exception as exc:
        log_event(
            logger,
            logging.WARNING,
            "health_probe_completion_failed",
            credential_id=credential_id,
            error_type=type(exc).__name__,
        )


async def health_probe_loop(
    interval_seconds: float,
    runtime_getter,
    recorder_getter,
    limiter_getter=lambda: None,
) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        runtime = runtime_getter()
        if runtime is not None:
            try:
                await run_health_probes(runtime, recorder_getter(), limiter_getter())
            except Exception as exc:
                log_event(
                    logger,
                    logging.WARNING,
                    "health_probe_sweep_failed",
                    error_type=type(exc).__name__,
                )
