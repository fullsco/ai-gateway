import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from gateway.alerts import AlertEvent, evaluate_alert_rules
from gateway.logging import log_event
from gateway.protocols import ClientProtocol

logger = logging.getLogger("gateway.observability")


@dataclass(frozen=True)
class PassiveHealthEvent:
    provider_id: str
    credential_id: str
    provider_model_id: str
    request_id: str
    attempt_number: int
    observed_at: datetime
    latency_ms: float
    error_category: str | None = None
    upstream_status: int | None = None
    retry_after_seconds: float | None = None
    source: str = "passive"
    # False when the failure is a provider/edge problem rather than evidence about
    # this credential. Such failures are still recorded, but must not mark the
    # credential unhealthy - otherwise one provider blip parks a working key.
    credential_at_fault: bool = True


@dataclass(frozen=True)
class UsageAttribution:
    provider_id: str
    provider_name: str | None
    provider_model_id: str
    route_id: str | None
    canonical_model: str
    upstream_model: str
    protocol: ClientProtocol
    pricing: dict[str, object] | None


class PassiveHealthRecorder:
    def __init__(self, pool: Any, *, max_queue_size: int = 1000) -> None:
        self._pool = pool
        self._queue: asyncio.Queue[PassiveHealthEvent | None] = asyncio.Queue(max_queue_size)
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    def submit(self, event: PassiveHealthEvent) -> None:
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            log_event(logger, logging.WARNING, "passive_health_event_dropped")

    async def close(self) -> None:
        if self._task is None:
            return
        await self._queue.put(None)
        await self._task
        self._task = None

    async def _run(self) -> None:
        while (event := await self._queue.get()) is not None:
            try:
                await self._persist(event)
            except Exception as exc:
                log_event(
                    logger,
                    logging.WARNING,
                    "passive_health_write_failed",
                    error_type=type(exc).__name__,
                )
            finally:
                self._queue.task_done()
        self._queue.task_done()

    async def _persist(self, event: PassiveHealthEvent) -> None:
        health = _passive_health(event.error_category)
        if event.source != "passive":
            await self._pool.execute(
                """
                insert into public.health_checks(
                  provider_id,credential_id,status,latency_ms,error_category,checked_at,source
                ) select $1,$2,$3::public.gateway_health_state,$4,$5,$6,$7
                  where $3::text is not null
                """,
                event.provider_id,
                event.credential_id,
                health,
                event.latency_ms,
                event.error_category,
                event.observed_at,
                event.source,
            )
            if event.error_category is not None:
                await self._alert(event)
            return
        if not event.credential_at_fault:
            # Observe it, but do not attribute it to the credential.
            await self._pool.execute(
                """
                insert into public.health_checks(
                  provider_id,credential_id,status,latency_ms,error_category,checked_at,source
                ) select $1,$2,$3::public.gateway_health_state,$4,$5,$6,$7
                  where $3::text is not null
                """,
                event.provider_id,
                event.credential_id,
                health,
                event.latency_ms,
                event.error_category,
                event.observed_at,
                event.source,
            )
            if event.error_category is not None:
                await self._alert(event)
            return
        cooldown_until = (
            event.observed_at + timedelta(seconds=event.retry_after_seconds or 30)
            if event.error_category == "rate_limit"
            else None
        )
        await self._pool.execute(
            """
            with previous as (
              select health::text as old_health from public.provider_credentials where id=$1
            ), updated as (
              update public.provider_credentials set
                last_used_at=greatest(coalesce(last_used_at,$2),$2),
                last_success_at=case when $3::text is null
                  then greatest(coalesce(last_success_at,$2),$2) else last_success_at end,
                last_failure_at=case when $3::text is not null
                  then greatest(coalesce(last_failure_at,$2),$2) else last_failure_at end,
                success_count=success_count + case when $3::text is null then 1 else 0 end,
                failure_count=failure_count + case when $3::text is not null then 1 else 0 end,
                health=case when $4::text is null then health
                  else $4::public.gateway_health_state end,
                cooldown_until=coalesce($5,cooldown_until),updated_at=now()
              where id=$1 returning health::text as new_health
            ), health as (
              insert into public.health_checks(
                provider_id,credential_id,status,latency_ms,error_category,checked_at,source
              ) select $6,$1,$4::public.gateway_health_state,$7,$3,$2,$12
                where $4::text is not null
            )
            insert into public.provider_events(provider_id,credential_id,event_type,metadata)
            select $6,$1,'health_transition',jsonb_build_object(
              'old_health',previous.old_health,'new_health',updated.new_health,
              'error_category',$3::text,'upstream_status',$8::integer,
              'latency_ms',$7::numeric,'attempt_number',$9::integer,
              'request_id',$10::text,'provider_model_id',$11::text
            ) from previous,updated
            where $4::text is not null and previous.old_health is distinct from updated.new_health
            """,
            event.credential_id,
            event.observed_at,
            event.error_category,
            health,
            cooldown_until,
            event.provider_id,
            event.latency_ms,
            event.upstream_status,
            event.attempt_number,
            event.request_id,
            event.provider_model_id,
            event.source,
        )
        if event.error_category is not None:
            await self._alert(event)

    async def _alert(self, event: PassiveHealthEvent) -> None:
        await evaluate_alert_rules(
            self._pool,
            AlertEvent(
                event_type="provider_failure",
                title=f"Provider request failed: {event.error_category}",
                scopes={
                    "provider": event.provider_id,
                    "credential": event.credential_id,
                    "route": event.provider_model_id,
                },
                metadata={
                    "error_category": event.error_category,
                    "upstream_status": event.upstream_status,
                    "latency_ms": event.latency_ms,
                },
            ),
        )


def _passive_health(error_category: str | None) -> str | None:
    return {
        None: "healthy",
        "upstream_authentication_error": "auth_failed",
        "upstream_waf_rejection": "degraded",
        "quota_exhausted": "quota_exhausted",
        "rate_limit": "rate_limited",
        "timeout": "degraded",
        "provider_unavailable": "unavailable",
    }.get(error_category)


class RequestRecorder:
    def __init__(self, pool: Any | None, request_id: str) -> None:
        self._pool = pool
        self.request_id = request_id

    async def start_request(
        self,
        *,
        client_id: str,
        key_id: str | None,
        protocol: ClientProtocol,
        requested_model: str,
        started_at: datetime,
    ) -> None:
        await self._execute(
            """
            insert into public.request_logs(
              id,client_id,key_id,protocol,requested_model,status,started_at
            ) values($1,$2,$3,$4,$5,'in_progress',$6)
            """,
            self.request_id,
            client_id,
            key_id,
            protocol.value,
            requested_model,
            started_at,
        )

    async def start_attempt(
        self,
        *,
        number: int,
        provider_id: str,
        credential_id: str,
        provider_model_id: str,
        started_at: datetime,
    ) -> int | None:
        return await self._fetchval(
            """
            insert into public.request_attempts(
              request_id,attempt_number,provider_id,credential_id,provider_model_id,
              status,started_at
            ) values($1,$2,$3,$4,$5,'in_progress',$6)
            returning id
            """,
            self.request_id,
            number,
            provider_id,
            credential_id,
            provider_model_id,
            started_at,
        )

    async def finish_attempt(
        self,
        attempt_id: int | None,
        *,
        status: str,
        ended_at: datetime,
        latency_ms: float,
        upstream_status: int | None = None,
        error_category: str | None = None,
        response_committed: bool = False,
    ) -> None:
        if attempt_id is None:
            return
        await self._execute(
            """
            update public.request_attempts set
              status=$2,upstream_status=$3,error_category=$4,response_committed=$5,
              ended_at=$6,latency_ms=$7
            where id=$1
            """,
            attempt_id,
            status,
            upstream_status,
            error_category,
            response_committed,
            ended_at,
            latency_ms,
        )

    async def finish_request(
        self,
        *,
        status: str,
        resolved_model: str | None,
        ended_at: datetime,
        latency_ms: float,
        retry_count: int,
        fallback_count: int,
        error_category: str | None = None,
    ) -> None:
        await self._execute(
            """
            update public.request_logs set
              status=$2,resolved_model=$3,ended_at=$4,latency_ms=$5,retry_count=$6,
              fallback_count=$7,error_category=$8
            where id=$1
            """,
            self.request_id,
            status,
            resolved_model,
            ended_at,
            latency_ms,
            retry_count,
            fallback_count,
            error_category,
        )

    async def record_routing_trace(self, traces: list[dict[str, Any]]) -> None:
        """Persist why each candidate was considered, excluded, or selected."""
        if not traces:
            return
        await self._execute(
            "update public.request_logs set routing_trace=$2::jsonb where id=$1",
            self.request_id,
            json.dumps(traces, default=str),
        )

    async def record_usage(
        self,
        attempt_id: int | None,
        protocol: ClientProtocol,
        content: bytes,
        attribution: UsageAttribution,
        *,
        attempt_status: str,
    ) -> None:
        if attempt_id is None:
            return
        usage = extract_usage(protocol, content)
        if usage is None:
            return
        await self.record_usage_values(
            attempt_id,
            usage,
            attribution,
            attempt_status=attempt_status,
        )

    async def record_usage_values(
        self,
        attempt_id: int | None,
        usage: tuple[int | None, int | None, int | None],
        attribution: UsageAttribution,
        *,
        attempt_status: str,
    ) -> None:
        if attempt_id is None:
            return
        if not _valid_usage(usage):
            log_event(logger, logging.WARNING, "invalid_usage_rejected")
            return
        estimated_cost, currency = estimate_cost(usage, attribution.pricing)
        pricing_context = dict(attribution.pricing or {}) if estimated_cost is not None else None
        pricing_context_hash = (
            hashlib.sha256(
                json.dumps(
                    pricing_context,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                ).encode()
            ).hexdigest()
            if pricing_context is not None
            else None
        )
        await self._execute(
            """
            insert into public.usage_records(
              request_id,attempt_id,input_tokens,output_tokens,cached_tokens,
              estimated_cost,currency,is_estimate,provider_id_snapshot,
              provider_name_snapshot,provider_model_id_snapshot,route_id_snapshot,
              canonical_model_snapshot,upstream_model_snapshot,protocol_snapshot,
              attempt_status_snapshot,pricing_context,pricing_context_hash
            ) values(
              $1,$2,$3,$4,$5,$6,$7,true,$8,$9,$10,$11,$12,$13,$14,$15,$16::jsonb,$17
            ) on conflict (attempt_id) do nothing
            """,
            self.request_id,
            attempt_id,
            usage[0],
            usage[1],
            usage[2],
            estimated_cost,
            currency,
            attribution.provider_id,
            attribution.provider_name,
            attribution.provider_model_id,
            attribution.route_id,
            attribution.canonical_model,
            attribution.upstream_model,
            attribution.protocol.value,
            attempt_status,
            json.dumps(pricing_context) if pricing_context is not None else None,
            pricing_context_hash,
        )

    async def _execute(self, query: str, *args: Any) -> None:
        if self._pool is None:
            return
        try:
            await self._pool.execute(query, *args)
        except Exception as exc:
            log_event(
                logger,
                logging.WARNING,
                "telemetry_write_failed",
                error_type=type(exc).__name__,
            )

    async def _fetchval(self, query: str, *args: Any) -> int | None:
        if self._pool is None:
            return None
        try:
            value = await self._pool.fetchval(query, *args)
            return int(value) if value is not None else None
        except Exception as exc:
            log_event(
                logger,
                logging.WARNING,
                "telemetry_write_failed",
                error_type=type(exc).__name__,
            )
            return None


def extract_usage(
    protocol: ClientProtocol,
    content: bytes,
) -> tuple[int | None, int | None, int | None] | None:
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict):
        return None
    if protocol is ClientProtocol.ANTHROPIC_MESSAGES:
        raw_values = (
            usage.get("input_tokens"),
            usage.get("output_tokens"),
            usage.get("cache_read_input_tokens"),
        )
    else:
        input_details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details")
        cached = input_details.get("cached_tokens") if isinstance(input_details, dict) else None
        raw_values = (
            usage.get("prompt_tokens", usage.get("input_tokens")),
            usage.get("completion_tokens", usage.get("output_tokens")),
            cached,
        )
    if any(value is not None and _integer(value) is None for value in raw_values):
        return None
    values = tuple(_integer(value) for value in raw_values)
    return values if any(value is not None for value in values) else None


def estimate_cost(
    usage: tuple[int | None, int | None, int | None],
    pricing: dict[str, object] | None,
) -> tuple[Decimal | None, str | None]:
    if not pricing:
        return None, None
    currency_value = pricing.get("currency")
    currency = str(currency_value).strip().upper() if currency_value is not None else ""
    if len(currency) != 3 or not currency.isalpha():
        return None, None
    input_tokens, output_tokens, cached_tokens = usage
    # A billable dimension that was never reported cannot be priced. Treating it
    # as zero produced an immutable, currency-stamped cost that looked measured,
    # counted toward pricing coverage, and understated the real spend. An absent
    # cached count is different: no cache read means nothing to discount.
    if input_tokens is None or output_tokens is None:
        return None, None
    cached_tokens = cached_tokens or 0
    if "blended_per_million" in pricing:
        # A measured blended rate is all a before/after billing measurement can
        # establish, so it is applied to total tokens and cannot discount cache
        # reads it never separated.
        try:
            blended_rate = Decimal(str(pricing["blended_per_million"]))
        except (InvalidOperation, TypeError, ValueError):
            return None, None
        if not blended_rate.is_finite() or blended_rate < 0:
            return None, None
        total = (
            Decimal(input_tokens + output_tokens) * blended_rate
        ) / Decimal(1_000_000)
        return total.quantize(Decimal("0.00000001")), currency
    try:
        input_rate = Decimal(str(pricing["input_per_million"]))
        output_rate = Decimal(str(pricing["output_per_million"]))
        cached_rate = Decimal(str(pricing.get("cached_input_per_million", input_rate)))
    except (KeyError, InvalidOperation, TypeError, ValueError):
        return None, None
    if min(input_rate, output_rate, cached_rate) < 0:
        return None, None
    uncached_input = max(0, input_tokens - cached_tokens)
    total = (
        Decimal(uncached_input) * input_rate
        + Decimal(output_tokens) * output_rate
        + Decimal(cached_tokens) * cached_rate
    ) / Decimal(1_000_000)
    return total.quantize(Decimal("0.00000001")), currency


def _valid_usage(usage: tuple[int | None, int | None, int | None]) -> bool:
    input_tokens, _, cached_tokens = usage
    return (
        any(value is not None for value in usage)
        and all(
        value is None or (isinstance(value, int) and not isinstance(value, bool) and value >= 0)
        for value in usage
        )
        and not (
            input_tokens is not None
            and cached_tokens is not None
            and cached_tokens > input_tokens
        )
    )


class StreamUsageAccumulator:
    MAX_EVENT_BYTES = 64 * 1024

    def __init__(self, protocol: ClientProtocol) -> None:
        self._protocol = protocol
        self._buffer = bytearray()
        self._usage: tuple[int | None, int | None, int | None] | None = None

    def feed(self, chunk: bytes) -> None:
        self._buffer.extend(chunk)
        while separator := self._separator():
            index, length = separator
            event = bytes(self._buffer[:index])
            del self._buffer[: index + length]
            self._consume_event(event)
        if len(self._buffer) > self.MAX_EVENT_BYTES:
            self._buffer.clear()

    @property
    def usage(self) -> tuple[int | None, int | None, int | None] | None:
        return self._usage

    def _separator(self) -> tuple[int, int] | None:
        crlf = self._buffer.find(b"\r\n\r\n")
        lf = self._buffer.find(b"\n\n")
        candidates = [(crlf, 4), (lf, 2)]
        present = [candidate for candidate in candidates if candidate[0] >= 0]
        return min(present) if present else None

    def _consume_event(self, event: bytes) -> None:
        data = b"\n".join(
            line.partition(b":")[2].lstrip()
            for line in event.splitlines()
            if line.startswith(b"data:")
        )
        if not data or data == b"[DONE]":
            return
        extracted = extract_usage(self._protocol, data)
        if extracted is None:
            return
        current = self._usage or (None, None, None)
        # Usage is cumulative within a stream, so keep the highest value seen for
        # each dimension. Merging on "is not None" let a later frame reporting 0
        # erase a real measurement: AgentRouter attaches a usage object to every
        # frame and sends all zeros on message_stop, which silently zeroed the
        # tokens for nearly all streamed Anthropic traffic.
        self._usage = tuple(
            _peak(existing, incoming)
            for existing, incoming in zip(current, extracted, strict=True)
        )


def _peak(existing: int | None, incoming: int | None) -> int | None:
    """Highest of two cumulative counters, tolerating either being unreported."""
    if incoming is None:
        return existing
    if existing is None:
        return incoming
    return max(existing, incoming)


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
