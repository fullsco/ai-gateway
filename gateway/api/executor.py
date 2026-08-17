import asyncio
import logging
from collections.abc import AsyncIterator, Mapping
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import httpx
from fastapi.responses import Response, StreamingResponse
from starlette.background import BackgroundTask

from gateway.alerts import AlertEvent, evaluate_alert_rules
from gateway.api.errors import client_error, gateway_error
from gateway.config import Settings
from gateway.context import get_request_id
from gateway.logging import log_event
from gateway.observability import (
    PassiveHealthEvent,
    PassiveHealthRecorder,
    RequestRecorder,
    StreamUsageAccumulator,
    UsageAttribution,
    estimate_cost,
)
from gateway.protocols import ClientProtocol, NormalizedRequest
from gateway.providers import ErrorCategory, ProviderError
from gateway.quotas import (
    BudgetExceeded,
    ProviderQuotaExceeded,
    QuotaExceeded,
    QuotaRequest,
    QuotaUnavailable,
    estimate_tokens,
    reserve_budgets,
    reserve_client_quota,
    reserve_provider_quota,
)
from gateway.routing import AttemptCoordinator, AttemptPolicy
from gateway.routing.controls import ConcurrencyLease
from gateway.routing.engine import NoRouteAvailable
from gateway.runtime import GatewayRuntime

logger = logging.getLogger("gateway.executor")


async def execute_request(
    normalized: NormalizedRequest,
    incoming_headers: Mapping[str, str],
    runtime: GatewayRuntime,
    settings: Settings,
    *,
    client_id: str,
    key_id: str | None = None,
    db_pool=None,
    health_recorder: PassiveHealthRecorder | None = None,
) -> Response:
    started_at = datetime.now(UTC)
    deadline = started_at + timedelta(seconds=settings.request_timeout_seconds)
    recorder = RequestRecorder(db_pool, get_request_id() or "unknown")
    await recorder.start_request(
        client_id=client_id,
        key_id=key_id,
        protocol=normalized.protocol,
        requested_model=normalized.requested_model,
        started_at=started_at,
    )
    try:
        client = runtime.key_store.get_client(client_id)
        if client is not None:
            await reserve_client_quota(
                db_pool,
                client_id,
                QuotaRequest(
                    requests_per_minute=client.requests_per_minute,
                    tokens_per_minute=client.tokens_per_minute,
                    estimated_tokens=estimate_tokens(normalized.payload),
                ),
            )
    except QuotaExceeded as exc:
        ended_at = datetime.now(UTC)
        await recorder.finish_request(
            status="failed", resolved_model=None, ended_at=ended_at,
            latency_ms=_latency_ms(started_at, ended_at), retry_count=0,
            fallback_count=0, error_category=ErrorCategory.RATE_LIMIT.value,
        )
        return client_error(ErrorCategory.RATE_LIMIT, str(exc), 429)
    except QuotaUnavailable as exc:
        ended_at = datetime.now(UTC)
        await recorder.finish_request(
            status="failed", resolved_model=None, ended_at=ended_at,
            latency_ms=_latency_ms(started_at, ended_at), retry_count=0,
            fallback_count=0, error_category=ErrorCategory.PROVIDER_UNAVAILABLE.value,
        )
        return client_error(ErrorCategory.PROVIDER_UNAVAILABLE, str(exc), 503)
    max_attempts = max(
        (
            int((model.routing_policy or {}).get("max_attempts", 3))
            for model in runtime.model_registry.list_provider_models()
        ),
        default=3,
    )
    coordinator = AttemptCoordinator(AttemptPolicy(max_attempts=max_attempts))
    estimated_input_tokens = estimate_tokens(normalized.payload)
    attempts = 0
    fallback_count = 0
    previous_provider_id: str | None = None
    excluded: frozenset[str] = frozenset()
    excluded_routes: frozenset[str] = frozenset()
    last_error: ProviderError | None = None

    while attempts < coordinator.policy.max_attempts:
        try:
            route = runtime.routing_engine.select(
                normalized,
                list(runtime.provider_states),
                list(runtime.credential_states),
                excluded_credential_ids=excluded,
                excluded_provider_model_ids=excluded_routes,
            )
        except (LookupError, NoRouteAvailable):
            break
        if not route.provider_model.allow_model_fallback:
            excluded_routes = excluded_routes | {
                candidate.id
                for candidate in runtime.model_registry.eligible_provider_models(normalized)
                if candidate.id != route.provider_model.id
            }
        if not await runtime.route_controls.allow(route.provider_model.id):
            excluded_routes = excluded_routes | {route.provider_model.id}
            continue
        adapter = runtime.provider_model_adapters[route.provider_model.id]
        credential = runtime.credentials[route.credential.credential_id]
        lease = await runtime.route_controls.acquire(
            route.provider_model.id,
            timeout_seconds=min(
                settings.concurrency_acquire_timeout_seconds,
                max(0, (deadline - datetime.now(UTC)).total_seconds()),
            ),
        )
        if lease is None:
            await runtime.route_controls.abandon(route.provider_model.id)
            last_error = ProviderError(
                category=ErrorCategory.PROVIDER_UNAVAILABLE,
                message="The selected provider route is at its concurrency limit.",
                retryable=True,
            )
            excluded_routes = excluded_routes | {route.provider_model.id}
            if not settings.failover_enabled or datetime.now(UTC) >= deadline:
                break
            continue
        estimated_output_tokens = normalized.payload.get(
            "max_tokens", normalized.payload.get("max_output_tokens")
        )
        output_tokens = (
            estimated_output_tokens
            if isinstance(estimated_output_tokens, int) and estimated_output_tokens >= 0
            else 0
        )
        estimated_cost, currency = estimate_cost(
            (estimated_input_tokens, output_tokens, None),
            dict(route.provider_model.pricing or {}),
        )
        try:
            await reserve_provider_quota(
                db_pool,
                route.credential.credential_id,
                QuotaRequest(
                    route.credential.requests_per_minute,
                    route.credential.tokens_per_minute,
                    estimated_input_tokens + output_tokens,
                ),
            )
            await reserve_budgets(
                db_pool,
                client_id=client_id,
                provider_id=route.provider_model.provider_id,
                credential_id=route.credential.credential_id,
                model_id=route.canonical_model_id,
                route_id=route.provider_model.route_id,
                currency=currency,
                estimated_cost=estimated_cost,
            )
        except (ProviderQuotaExceeded, BudgetExceeded) as exc:
            lease.release()
            event_type = "budget_exceeded" if isinstance(exc, BudgetExceeded) else "provider_quota"
            await evaluate_alert_rules(
                db_pool,
                AlertEvent(
                    event_type=event_type,
                    title=str(exc),
                    scopes={
                        "client": client_id,
                        "provider": route.provider_model.provider_id,
                        "credential": route.credential.credential_id,
                        "model": route.canonical_model_id,
                        "route": route.provider_model.route_id or route.provider_model.id,
                    },
                    metadata={"reason": str(exc)},
                ),
            )
            last_error = ProviderError(
                category=ErrorCategory.QUOTA_EXHAUSTED,
                message=str(exc),
                retryable=True,
            )
            if isinstance(exc, BudgetExceeded):
                excluded_routes = excluded_routes | {route.provider_model.id}
            else:
                excluded = excluded | {route.credential.credential_id}
            continue
        except QuotaUnavailable as exc:
            lease.release()
            ended_at = datetime.now(UTC)
            await recorder.finish_request(
                status="failed",
                resolved_model=None,
                ended_at=ended_at,
                latency_ms=_latency_ms(started_at, ended_at),
                retry_count=max(0, attempts - 1),
                fallback_count=fallback_count,
                error_category=ErrorCategory.PROVIDER_UNAVAILABLE.value,
            )
            return client_error(ErrorCategory.PROVIDER_UNAVAILABLE, str(exc), 503)
        try:
            upstream = adapter.create_request(
                _map_upstream_model(normalized, route.provider_model.upstream_model_id),
                credential,
                dict(incoming_headers),
            )
            if runtime.http_client is None:
                raise RuntimeError("Gateway runtime has no HTTP client")
            built = runtime.http_client.build_request(
                upstream.method,
                upstream.url,
                headers=upstream.headers,
                json=upstream.json_body,
                timeout=upstream.timeout,
            )
        except Exception as exc:
            lease.release()
            ended_at = datetime.now(UTC)
            log_event(
                logger,
                logging.ERROR,
                "upstream_request_build_failed",
                provider_id=route.provider_model.provider_id,
                provider_model_id=route.provider_model.id,
                error_type=type(exc).__name__,
            )
            await recorder.finish_request(
                status="failed",
                resolved_model=None,
                ended_at=ended_at,
                latency_ms=_latency_ms(started_at, ended_at),
                retry_count=max(0, attempts - 1),
                fallback_count=fallback_count,
                error_category=ErrorCategory.PROVIDER_UNAVAILABLE.value,
            )
            return client_error(
                ErrorCategory.PROVIDER_UNAVAILABLE,
                "The gateway could not prepare the upstream request.",
                503,
            )
        response: httpx.Response | None = None
        attempts += 1
        if (
            previous_provider_id is not None
            and previous_provider_id != route.provider_model.provider_id
        ):
            fallback_count += 1
        previous_provider_id = route.provider_model.provider_id
        attempt_started_at = datetime.now(UTC)
        pricing_snapshot = deepcopy(dict(route.provider_model.pricing or {}))
        attribution = UsageAttribution(
            provider_id=route.provider_model.provider_id,
            provider_name=route.provider_model.provider_name,
            provider_model_id=route.provider_model.id,
            route_id=route.provider_model.route_id,
            canonical_model=route.canonical_model_id,
            upstream_model=route.provider_model.upstream_model_id,
            protocol=route.provider_model.protocol,
            pricing=pricing_snapshot,
        )
        attempt_id = await recorder.start_attempt(
            number=attempts,
            provider_id=route.provider_model.provider_id,
            credential_id=route.credential.credential_id,
            provider_model_id=route.provider_model.id,
            started_at=attempt_started_at,
        )
        try:
            response = await runtime.http_client.send(built, stream=True)
            if response.status_code >= 400:
                await response.aread()
                last_error = adapter.normalize_error(response)
                await recorder.record_usage(
                    attempt_id,
                    normalized.protocol,
                    response.content,
                    attribution,
                    attempt_status="failed",
                )
                await response.aclose()
                response = None
            elif not _valid_upstream_content_type(response, normalized.stream):
                await response.aread()
                last_error = ProviderError(
                    category=ErrorCategory.UPSTREAM_WAF_REJECTION,
                    message="The upstream provider returned an unexpected response format.",
                    retryable=True,
                    upstream_status=response.status_code,
                )
                await recorder.record_usage(
                    attempt_id,
                    normalized.protocol,
                    response.content,
                    attribution,
                    attempt_status="failed",
                )
                await response.aclose()
                response = None
            elif normalized.stream:
                iterator = response.aiter_raw()
                try:
                    first_chunk = await asyncio.wait_for(
                        anext(iterator), timeout=settings.first_event_timeout_seconds
                    )
                except StopAsyncIteration:
                    last_error = ProviderError(
                        category=ErrorCategory.PROVIDER_UNAVAILABLE,
                        message="The upstream provider closed the stream before sending data.",
                        retryable=True,
                    )
                    await _close_response(response)
                    response = None
                else:
                    finalizer = _StreamFinalizer(
                        recorder,
                        response=response,
                        attempt_id=attempt_id,
                        protocol=normalized.protocol,
                        resolved_model=route.canonical_model_id,
                        started_at=started_at,
                        attempt_started_at=attempt_started_at,
                        attempts=attempts,
                        fallback_count=fallback_count,
                        route_controls=runtime.route_controls,
                        route_id=route.provider_model.id,
                        lease=lease,
                        health_recorder=health_recorder,
                        provider_id=route.provider_model.provider_id,
                        credential_id=route.credential.credential_id,
                        provider_model_id=route.provider_model.id,
                        attempt_number=attempts,
                        attribution=attribution,
                    )
                    return StreamingResponse(
                        _stream_response(
                            first_chunk,
                            iterator,
                            finalizer,
                        ),
                        status_code=200,
                        media_type=response.headers.get("content-type", "text/event-stream"),
                        background=BackgroundTask(finalizer.finish, False),
                    )
            else:
                content = await response.aread()
                headers = _safe_response_headers(response)
                status_code = response.status_code
                await response.aclose()
                lease.release()
                await runtime.route_controls.record_success(route.provider_model.id)
                ended_at = datetime.now(UTC)
                latency_ms = _latency_ms(attempt_started_at, ended_at)
                _submit_health(
                    health_recorder,
                    recorder,
                    route.provider_model.provider_id,
                    route.credential.credential_id,
                    route.provider_model.id,
                    attempts,
                    ended_at,
                    latency_ms,
                    upstream_status=status_code,
                )
                await recorder.finish_attempt(
                    attempt_id,
                    status="succeeded",
                    ended_at=ended_at,
                    latency_ms=latency_ms,
                    upstream_status=status_code,
                )
                await recorder.record_usage(
                    attempt_id,
                    normalized.protocol,
                    content,
                    attribution,
                    attempt_status="succeeded",
                )
                await recorder.finish_request(
                    status="succeeded",
                    resolved_model=route.canonical_model_id,
                    ended_at=ended_at,
                    latency_ms=_latency_ms(started_at, ended_at),
                    retry_count=attempts - 1,
                    fallback_count=fallback_count,
                )
                return Response(content, status_code=status_code, headers=headers)
        except (TimeoutError, httpx.TransportError) as exc:
            last_error = _transport_error(exc)
            log_event(
                logger,
                logging.WARNING,
                "upstream_transport_failed",
                provider_id=route.provider_model.provider_id,
                provider_model_id=route.provider_model.id,
                attempt_number=attempts,
                duration_ms=_latency_ms(attempt_started_at, datetime.now(UTC)),
                transport_error_type=type(exc).__name__,
                transport_cause_type=(
                    type(exc.__cause__).__name__ if exc.__cause__ is not None else None
                ),
            )
            await _close_response(response)
        except asyncio.CancelledError:
            await _close_response(response)
            await runtime.route_controls.abandon(route.provider_model.id)
            ended_at = datetime.now(UTC)
            lease.release()
            await asyncio.shield(
                _finish_cancelled_request(
                    recorder,
                    attempt_id=attempt_id,
                    attempt_started_at=attempt_started_at,
                    started_at=started_at,
                    ended_at=ended_at,
                    attempts=attempts,
                    fallback_count=fallback_count,
                )
            )
            raise
        except Exception as exc:
            last_error = ProviderError(
                category=ErrorCategory.PROVIDER_UNAVAILABLE,
                message="The gateway failed while dispatching to the upstream provider.",
                retryable=True,
            )
            log_event(
                logger,
                logging.ERROR,
                "upstream_dispatch_failed",
                provider_id=route.provider_model.provider_id,
                provider_model_id=route.provider_model.id,
                attempt_number=attempts,
                error_type=type(exc).__name__,
            )
            await _close_response(response)

        if last_error is not None:
            ended_at = datetime.now(UTC)
            await recorder.finish_attempt(
                attempt_id,
                status="failed",
                ended_at=ended_at,
                latency_ms=_latency_ms(attempt_started_at, ended_at),
                upstream_status=last_error.upstream_status,
                error_category=last_error.category.value,
            )
            if _affects_circuit(last_error):
                await runtime.route_controls.record_failure(route.provider_model.id)
            else:
                await runtime.route_controls.abandon(route.provider_model.id)
            _submit_health(
                health_recorder,
                recorder,
                route.provider_model.provider_id,
                route.credential.credential_id,
                route.provider_model.id,
                attempts,
                ended_at,
                _latency_ms(attempt_started_at, ended_at),
                error=last_error,
            )
        lease.release()

        excluded = excluded | {route.credential.credential_id}
        if (
            not settings.failover_enabled
            or last_error is None
            or not coordinator.should_retry(
            error=last_error,
            attempts_made=attempts,
            response_committed=False,
            now=datetime.now(UTC),
            deadline=deadline,
            )
        ):
            break

    if last_error is not None:
        ended_at = datetime.now(UTC)
        await recorder.finish_request(
            status="failed",
            resolved_model=None,
            ended_at=ended_at,
            latency_ms=_latency_ms(started_at, ended_at),
            retry_count=max(0, attempts - 1),
            fallback_count=fallback_count,
            error_category=last_error.category.value,
        )
        return gateway_error(last_error)
    ended_at = datetime.now(UTC)
    await recorder.finish_request(
        status="failed",
        resolved_model=None,
        ended_at=ended_at,
        latency_ms=_latency_ms(started_at, ended_at),
        retry_count=0,
        fallback_count=0,
        error_category=ErrorCategory.MODEL_UNAVAILABLE.value,
    )
    return client_error(ErrorCategory.MODEL_UNAVAILABLE, "No eligible model route exists.", 503)


def _transport_error(exc: Exception) -> ProviderError:
    if isinstance(exc, httpx.TimeoutException):
        return ProviderError(
            category=ErrorCategory.TIMEOUT,
            message="The upstream provider timed out.",
            retryable=True,
        )
    return ProviderError(
        category=ErrorCategory.PROVIDER_UNAVAILABLE,
        message="The upstream provider is unavailable.",
        retryable=True,
    )


def _valid_upstream_content_type(response: httpx.Response, streaming: bool) -> bool:
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if streaming:
        return content_type == "text/event-stream"
    return content_type == "application/json" or content_type.endswith("+json")


def _map_upstream_model(request: NormalizedRequest, upstream_model: str) -> NormalizedRequest:
    payload = deepcopy(request.payload)
    payload["model"] = upstream_model
    return request.model_copy(update={"payload": payload})


async def _close_response(response: httpx.Response | None) -> None:
    if response is not None:
        await response.aclose()


async def _stream_response(
    first_chunk: bytes,
    iterator: AsyncIterator[bytes],
    finalizer: "_StreamFinalizer",
) -> AsyncIterator[bytes]:
    completed = False
    try:
        finalizer.usage.feed(first_chunk)
        yield first_chunk
        async for chunk in iterator:
            finalizer.usage.feed(chunk)
            yield chunk
        completed = True
    finally:
        await finalizer.finish(completed)


class _StreamFinalizer:
    def __init__(
        self,
        recorder: RequestRecorder,
        *,
        response: httpx.Response,
        attempt_id: int | None,
        protocol: ClientProtocol,
        resolved_model: str,
        started_at: datetime,
        attempt_started_at: datetime,
        attempts: int,
        fallback_count: int,
        route_controls=None,
        route_id: str | None = None,
        lease: ConcurrencyLease | None = None,
        health_recorder: PassiveHealthRecorder | None = None,
        provider_id: str | None = None,
        credential_id: str | None = None,
        provider_model_id: str | None = None,
        attempt_number: int = 1,
        attribution: UsageAttribution | None = None,
    ) -> None:
        self.recorder = recorder
        self.response = response
        self.attempt_id = attempt_id
        self.resolved_model = resolved_model
        self.started_at = started_at
        self.attempt_started_at = attempt_started_at
        self.attempts = attempts
        self.fallback_count = fallback_count
        self.route_controls = route_controls
        self.route_id = route_id
        self.lease = lease
        self.health_recorder = health_recorder
        self.provider_id = provider_id
        self.credential_id = credential_id
        self.provider_model_id = provider_model_id
        self.attempt_number = attempt_number
        self.attribution = attribution
        self.usage = StreamUsageAccumulator(protocol)
        self._task: asyncio.Task[None] | None = None

    async def finish(self, completed: bool) -> None:
        if self._task is None:
            ended_at = datetime.now(UTC)
            self._task = asyncio.create_task(
                _finalize_stream(
                    self.recorder,
                    response=self.response,
                    attempt_id=self.attempt_id,
                    completed=completed,
                    usage=self.usage,
                    resolved_model=self.resolved_model,
                    started_at=self.started_at,
                    attempt_started_at=self.attempt_started_at,
                    ended_at=ended_at,
                    attempts=self.attempts,
                    fallback_count=self.fallback_count,
                    upstream_status=self.response.status_code,
                    route_controls=self.route_controls,
                    route_id=self.route_id,
                    lease=self.lease,
                    health_recorder=self.health_recorder,
                    provider_id=self.provider_id,
                    credential_id=self.credential_id,
                    provider_model_id=self.provider_model_id,
                    attempt_number=self.attempt_number,
                    attribution=self.attribution,
                )
            )
        await asyncio.shield(self._task)


async def _finalize_stream(
    recorder: RequestRecorder,
    *,
    response: httpx.Response,
    attempt_id: int | None,
    completed: bool,
    usage: StreamUsageAccumulator,
    resolved_model: str,
    started_at: datetime,
    attempt_started_at: datetime,
    ended_at: datetime,
    attempts: int,
    fallback_count: int,
    upstream_status: int,
    route_controls=None,
    route_id: str | None = None,
    lease: ConcurrencyLease | None = None,
    health_recorder: PassiveHealthRecorder | None = None,
    provider_id: str | None = None,
    credential_id: str | None = None,
    provider_model_id: str | None = None,
    attempt_number: int = 1,
    attribution: UsageAttribution | None = None,
) -> None:
    try:
        await response.aclose()
    finally:
        if lease is not None:
            lease.release()
    if route_controls is not None and route_id is not None:
        if completed:
            await route_controls.record_success(route_id)
        else:
            await route_controls.abandon(route_id)
    if completed and provider_id and credential_id and provider_model_id:
        _submit_health(
            health_recorder,
            recorder,
            provider_id,
            credential_id,
            provider_model_id,
            attempt_number,
            ended_at,
            _latency_ms(attempt_started_at, ended_at),
            upstream_status=upstream_status,
        )
    await recorder.finish_attempt(
        attempt_id,
        status="succeeded" if completed else "cancelled",
        ended_at=ended_at,
        latency_ms=_latency_ms(attempt_started_at, ended_at),
        upstream_status=upstream_status,
        response_committed=True,
    )
    if completed and usage.usage is not None and attribution is not None:
        await recorder.record_usage_values(
            attempt_id,
            usage.usage,
            attribution,
            attempt_status="succeeded",
        )
    await recorder.finish_request(
        status="succeeded" if completed else "cancelled",
        resolved_model=resolved_model,
        ended_at=ended_at,
        latency_ms=_latency_ms(started_at, ended_at),
        retry_count=attempts - 1,
        fallback_count=fallback_count,
    )


def _safe_response_headers(response: httpx.Response) -> dict[str, str]:
    allowed = {
        "content-type",
        "request-id",
        "anthropic-request-id",
        "openai-request-id",
        "x-request-id",
    }
    return {key: value for key, value in response.headers.items() if key.lower() in allowed}


def _latency_ms(started_at: datetime, ended_at: datetime) -> float:
    return round((ended_at - started_at).total_seconds() * 1000, 3)


async def _finish_cancelled_request(
    recorder: RequestRecorder,
    *,
    attempt_id: int | None,
    attempt_started_at: datetime,
    started_at: datetime,
    ended_at: datetime,
    attempts: int,
    fallback_count: int,
) -> None:
    await recorder.finish_attempt(
        attempt_id,
        status="cancelled",
        ended_at=ended_at,
        latency_ms=_latency_ms(attempt_started_at, ended_at),
    )
    await recorder.finish_request(
        status="cancelled",
        resolved_model=None,
        ended_at=ended_at,
        latency_ms=_latency_ms(started_at, ended_at),
        retry_count=max(0, attempts - 1),
        fallback_count=fallback_count,
        error_category=ErrorCategory.PROVIDER_UNAVAILABLE.value,
    )


def _affects_circuit(error: ProviderError) -> bool:
    return error.category in {ErrorCategory.PROVIDER_UNAVAILABLE, ErrorCategory.TIMEOUT}


def _submit_health(
    health_recorder: PassiveHealthRecorder | None,
    request_recorder: RequestRecorder,
    provider_id: str,
    credential_id: str,
    provider_model_id: str,
    attempt_number: int,
    observed_at: datetime,
    latency_ms: float,
    *,
    error: ProviderError | None = None,
    upstream_status: int | None = None,
) -> None:
    if health_recorder is None:
        return
    health_recorder.submit(
        PassiveHealthEvent(
            provider_id=provider_id,
            credential_id=credential_id,
            provider_model_id=provider_model_id,
            request_id=request_recorder.request_id,
            attempt_number=attempt_number,
            observed_at=observed_at,
            latency_ms=latency_ms,
            error_category=error.category.value if error else None,
            upstream_status=error.upstream_status if error else upstream_status,
            retry_after_seconds=error.retry_after_seconds if error else None,
        )
    )
