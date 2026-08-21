import asyncio
import functools
import json
import logging
from collections.abc import AsyncIterator, Mapping
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal

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
from gateway.providers import ErrorCategory, ProviderError, build_provider_error
from gateway.providers.base import RetryScope
from gateway.quotas import (
    BudgetExceeded,
    ClientSpendingLimitExceeded,
    ProviderQuotaExceeded,
    QuotaExceeded,
    QuotaRequest,
    QuotaUnavailable,
    estimate_tokens,
    reserve_budgets,
    reserve_client_quota,
    reserve_client_spending,
    reserve_provider_quota,
    settle_budgets,
    settle_client_spending,
)
from gateway.routing import AttemptCoordinator, AttemptPolicy
from gateway.routing.controls import ConcurrencyLease
from gateway.routing.engine import NoRouteAvailable, RoutingTrace
from gateway.routing.live_state import LiveOperationalState
from gateway.runtime import GatewayRuntime

logger = logging.getLogger("gateway.executor")


async def _settle_reservation(
    db_pool,
    *,
    client_id: str,
    route,
    currency: str | None,
    reserved,
    actual,
) -> None:
    """Correct the pre-flight reservation to what the request actually cost.

    The reservation is taken before dispatch, with the output side priced at the
    client's declared max_tokens rather than its outcome, so it always overstates.
    Leaving it uncorrected made reserved spend drift permanently above real spend:
    measured at 1.88x on live traffic, which would have made a $4,000 budget start
    refusing requests at about $2,124.
    """
    if reserved is None or currency is None:
        return
    delta = (actual or Decimal(0)) - reserved
    if delta == 0:
        return
    await settle_budgets(
        db_pool,
        client_id=client_id,
        provider_id=route.provider_model.provider_id,
        credential_id=route.credential.credential_id,
        model_id=route.canonical_model_id,
        route_id=route.provider_model.route_id,
        currency=currency,
        delta=delta,
    )
    await settle_client_spending(db_pool, client_id, delta)


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
    live_state: LiveOperationalState | None = None,
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
    # Bound by the routes that can actually serve THIS model. Taking the maximum
    # across every provider model in the registry meant one route configured with
    # max_attempts 10 raised the retry budget for every model in the gateway.
    try:
        requested_canonical = runtime.model_registry.resolve(normalized.requested_model).id
    except LookupError:
        requested_canonical = None
    candidate_models = [
        model
        for model in runtime.model_registry.list_provider_models()
        if requested_canonical is not None
        and model.canonical_model_id == requested_canonical
    ] or runtime.model_registry.list_provider_models()
    max_attempts = max(
        (
            int((model.routing_policy or {}).get("max_attempts", 3))
            for model in candidate_models
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
    no_candidates = False

    traces: list[dict] = []
    while attempts < coordinator.policy.max_attempts:
        # Configuration comes from the published snapshot; health, cooldown, quota,
        # rate headroom and latency come from live runtime state so a failing
        # credential is avoided immediately, without publishing a new snapshot.
        if live_state is not None:
            provider_states, credential_states, diagnostics = live_state.overlay(
                runtime.provider_states, runtime.credential_states
            )
        else:
            provider_states = list(runtime.provider_states)
            credential_states = list(runtime.credential_states)
            diagnostics = {}
        trace = RoutingTrace(attempt_number=attempts + 1)
        if attempts:
            trace.is_fallback = True
            trace.fallback_reason = (
                last_error.category.value if last_error is not None else "retry"
            )
        try:
            route = runtime.routing_engine.select(
                normalized,
                provider_states,
                credential_states,
                excluded_credential_ids=excluded,
                excluded_provider_model_ids=excluded_routes,
                trace=trace,
                diagnostics=diagnostics,
            )
        except (LookupError, NoRouteAvailable) as exc:
            trace.selected = None
            trace.fallback_reason = trace.fallback_reason or type(exc).__name__
            # Whether any candidate was even considered separates a configuration
            # problem from exhausted capacity. Nothing considered means no mapping
            # matches the request at all, for example the model is not exposed over
            # the requested protocol. Candidates considered and all excluded means
            # the model is servable but everything was temporarily ineligible.
            no_candidates = not trace.considered
            traces.append(trace.as_dict())
            break
        traces.append(trace.as_dict())
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
            last_error = build_provider_error(
                ErrorCategory.PROVIDER_UNAVAILABLE,
                "The selected provider route is at its concurrency limit.",
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
        # Pre-flight reservation: nothing is known about caching yet, so both
        # cached dimensions are unreported and the whole input is priced fresh.
        estimated_cost, currency = estimate_cost(
            (estimated_input_tokens, output_tokens, None, None),
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
            await reserve_client_spending(db_pool, client_id, estimated_cost)
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
            if isinstance(exc, ClientSpendingLimitExceeded):
                event_type = "client_spending_limit"
            elif isinstance(exc, BudgetExceeded):
                event_type = "budget_exceeded"
            else:
                event_type = "provider_quota"
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
            last_error = build_provider_error(ErrorCategory.QUOTA_EXHAUSTED, str(exc))
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
                actual_cost = await recorder.record_usage(
                    attempt_id,
                    normalized.protocol,
                    response.content,
                    attribution,
                    attempt_status="failed",
                )
                # A rejected attempt still costs whatever the upstream billed, which
                # is usually nothing. Settle so the reservation does not linger.
                await _settle_reservation(
                    db_pool, client_id=client_id, route=route, currency=currency,
                    reserved=estimated_cost, actual=actual_cost,
                )
                await response.aclose()
                response = None
            elif normalized.stream:
                iterator = response.aiter_raw()
                try:
                    first_chunk = (
                        response.content
                        if response.is_stream_consumed
                        else await asyncio.wait_for(
                            anext(iterator), timeout=settings.first_event_timeout_seconds
                        )
                    )
                except StopAsyncIteration:
                    last_error = build_provider_error(
                        ErrorCategory.PROVIDER_UNAVAILABLE,
                        "The upstream provider closed the stream before sending data.",
                    )
                    await _close_response(response)
                    response = None
                else:
                    if not _valid_upstream_stream(response, first_chunk):
                        last_error = _unexpected_upstream_response(response.status_code)
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
                            settle=functools.partial(
                                _settle_reservation,
                                db_pool,
                                client_id=client_id,
                                route=route,
                                currency=currency,
                                reserved=estimated_cost,
                            ),
                        )
                        if live_state is not None:
                            live_state.record_attempt(
                                provider_id=route.provider_model.provider_id,
                                credential_id=route.credential.credential_id,
                                succeeded=True,
                                latency_ms=_latency_ms(
                                    attempt_started_at, datetime.now(UTC)
                                ),
                            )
                        return StreamingResponse(
                            _stream_response(
                                first_chunk,
                                iterator,
                                finalizer,
                            ),
                            status_code=200,
                            media_type="text/event-stream",
                            background=BackgroundTask(finalizer.finish, False),
                        )
            else:
                content = await response.aread()
                if not _valid_upstream_json(content):
                    last_error = _unexpected_upstream_response(response.status_code)
                    await response.aclose()
                    response = None
                else:
                    headers = _safe_response_headers(response)
                    status_code = response.status_code
                    await response.aclose()
                    lease.release()
                    await runtime.route_controls.record_success(route.provider_model.id)
                    ended_at = datetime.now(UTC)
                    latency_ms = _latency_ms(attempt_started_at, ended_at)
                    if live_state is not None:
                        live_state.record_attempt(
                            provider_id=route.provider_model.provider_id,
                            credential_id=route.credential.credential_id,
                            succeeded=True,
                            latency_ms=latency_ms,
                        )
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
                    actual_cost = await recorder.record_usage(
                        attempt_id,
                        normalized.protocol,
                        content,
                        attribution,
                        attempt_status="succeeded",
                    )
                    await _settle_reservation(
                        db_pool, client_id=client_id, route=route, currency=currency,
                        reserved=estimated_cost, actual=actual_cost,
                    )
                    await recorder.finish_request(
                        status="succeeded",
                        resolved_model=route.canonical_model_id,
                        ended_at=ended_at,
                        latency_ms=_latency_ms(started_at, ended_at),
                        retry_count=attempts - 1,
                        fallback_count=fallback_count,
                    )
                    await recorder.record_routing_trace(traces)
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
            last_error = build_provider_error(
                ErrorCategory.PROVIDER_UNAVAILABLE,
                "The gateway failed while dispatching to the upstream provider.",
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

        # Feed the outcome back into live state immediately so the very next
        # selection (this request's retry, and every later request) already
        # reflects it. No configuration publish is involved.
        if live_state is not None:
            live_state.record_attempt(
                provider_id=route.provider_model.provider_id,
                credential_id=route.credential.credential_id,
                succeeded=last_error is None,
                error_category=last_error.category.value if last_error else None,
                latency_ms=_latency_ms(attempt_started_at, datetime.now(UTC)),
                retry_after_seconds=last_error.retry_after_seconds if last_error else None,
                credential_at_fault=last_error.credential_at_fault if last_error else True,
            )

        # Always retire the credential that just failed for this request.
        excluded = excluded | {route.credential.credential_id}
        # A provider-scoped failure is not the credential's fault, so a sibling key
        # on the same provider is unlikely to help: the upstream is down, WAF-blocked
        # or does not serve the model. Prefer a different provider. Without this,
        # retry_scope was computed and never acted on, so PROVIDER behaved exactly
        # like CREDENTIAL: a provider with more credentials than the attempt budget
        # consumed every attempt before any configured fallback was reached.
        # GoRouter has five credentials against three attempts, which made its
        # TabiAi fallback unreachable in production.
        #
        # Only retire the provider when another provider is actually reachable for
        # this request. If it is the only route left, whether because nothing else
        # maps this model or because allow_model_fallback forbids the alternatives,
        # then a sibling key is the last remaining chance and is worth spending an
        # attempt on. Retiring the provider unconditionally would convert a
        # recoverable single-provider failure into a hard error.
        if last_error is not None and last_error.retry_scope is RetryScope.PROVIDER:
            sibling_routes = {
                model.id
                for model in runtime.model_registry.list_provider_models()
                if model.provider_id == route.provider_model.provider_id
            }
            another_provider_is_reachable = any(
                candidate.id not in excluded_routes and candidate.id not in sibling_routes
                for candidate in runtime.model_registry.eligible_provider_models(normalized)
            )
            if another_provider_is_reachable:
                excluded_routes = excluded_routes | sibling_routes
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
        await recorder.record_routing_trace(traces)
        return gateway_error(last_error)
    ended_at = datetime.now(UTC)
    category = (
        ErrorCategory.MODEL_UNAVAILABLE if no_candidates
        else ErrorCategory.NO_ELIGIBLE_ROUTE
    )
    await recorder.finish_request(
        status="failed",
        resolved_model=None,
        ended_at=ended_at,
        latency_ms=_latency_ms(started_at, ended_at),
        retry_count=0,
        fallback_count=0,
        error_category=category.value,
    )
    await recorder.record_routing_trace(traces)
    if category is ErrorCategory.MODEL_UNAVAILABLE:
        return client_error(
            category,
            "No provider is configured to serve this model as requested.",
            404,
        )
    # The model is configured; nothing that could serve it was eligible right now.
    # Reporting this as model_unavailable told the operator the model was missing,
    # which sent them to look at mappings instead of at health, quota and cooldown.
    return client_error(
        category,
        "No provider route for this model is currently eligible. The routing trace "
        "records why each candidate was excluded.",
        503,
    )


def _transport_error(exc: Exception) -> ProviderError:
    if isinstance(exc, httpx.TimeoutException):
        return build_provider_error(
            ErrorCategory.TIMEOUT, "The upstream provider timed out."
        )
    return build_provider_error(
        ErrorCategory.PROVIDER_UNAVAILABLE, "The upstream provider is unavailable."
    )


def _valid_upstream_json(content: bytes) -> bool:
    try:
        return isinstance(json.loads(content), dict)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False


def _valid_upstream_stream(response: httpx.Response, chunk: bytes) -> bool:
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type == "text/event-stream":
        return True
    first_line = chunk.lstrip().splitlines()[0] if chunk.strip() else b""
    return first_line.startswith((b"data:", b"event:", b"id:", b"retry:", b":"))


def _unexpected_upstream_response(status_code: int) -> ProviderError:
    return build_provider_error(
        ErrorCategory.UPSTREAM_WAF_REJECTION,
        "The upstream provider returned an unexpected response format.",
        upstream_status=status_code,
    )


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
        settle=None,
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
        # Called with the actual cost once the stream ends, so the pre-flight
        # reservation can be corrected. A stream only knows its usage at the end.
        self.settle = settle
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
                    settle=self.settle,
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
    settle=None,
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
    actual_cost = None
    if completed and usage.usage is not None and attribution is not None:
        actual_cost = await recorder.record_usage_values(
            attempt_id,
            usage.usage,
            attribution,
            attempt_status="succeeded",
        )
    if settle is not None:
        # A cancelled stream reports no usage, so the whole reservation is released.
        await settle(actual=actual_cost)
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
            credential_at_fault=error.credential_at_fault if error else True,
        )
    )
