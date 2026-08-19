import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Any

from gateway.logging import log_event

logger = logging.getLogger("gateway.health.limits")


@dataclass(frozen=True)
class ProbeLimitConfig:
    daily_limit: int = 10
    min_interval_seconds: int = 7200
    lease_seconds: int = 30
    failure_backoff_seconds: int = 7200
    max_backoff_seconds: int = 86400
    manual_daily_limit: int = 20
    manual_min_interval_seconds: int = 60
    # Real traffic already reveals credential health, and an Anthropic-protocol
    # probe is a billable POST /v1/messages. Skip the probe entirely while a
    # credential has been exercised this recently.
    passive_signal_window_seconds: int = 7200


_RECENT_PASSIVE_SIGNAL = """
select 1 from public.provider_credentials
where id = $1 and last_used_at is not null
  and last_used_at >= now() - make_interval(secs => $2)
"""


class HealthProbeLimiter:
    def __init__(self, pool: Any, config: ProbeLimitConfig) -> None:
        self._pool = pool
        self._config = config

    def route_index(self, credential_id: str, route_count: int) -> int:
        if route_count <= 1:
            return 0
        seed = int.from_bytes(
            hashlib.blake2s(credential_id.encode(), digest_size=4).digest(), "big"
        )
        slot = int(time.time() // self._config.min_interval_seconds)
        return (seed + slot) % route_count

    async def reserve(
        self,
        provider_id: str,
        credential_id: str,
        provider_model_id: str,
        *,
        manual: bool = False,
    ) -> str:
        if not manual and self._config.passive_signal_window_seconds > 0:
            recent = await self._pool.fetchval(
                _RECENT_PASSIVE_SIGNAL,
                credential_id,
                self._config.passive_signal_window_seconds,
            )
            if recent:
                await self._record_skip(
                    provider_id, credential_id, provider_model_id, "recent_passive_signal"
                )
                return "recent_passive_signal"
        reason = await self._pool.fetchval(
            "select public.reserve_health_probe($1,$2,$3,$4,$5,$6,$7,$8,$9)",
            provider_id,
            credential_id,
            provider_model_id,
            self._config.daily_limit,
            self._config.min_interval_seconds,
            self._config.lease_seconds,
            manual,
            self._config.manual_daily_limit,
            self._config.manual_min_interval_seconds,
        )
        result = str(reason)
        if not result.startswith("reserved:"):
            await self._record_skip(provider_id, credential_id, provider_model_id, result)
        return result

    async def complete(
        self, credential_id: str, reservation_token: str, *, success: bool, result: str
    ) -> None:
        await self._pool.execute(
            "select public.complete_health_probe($1,$2,$3,$4,$5,$6,$7)",
            credential_id,
            success,
            self._config.min_interval_seconds,
            self._config.failure_backoff_seconds,
            self._config.max_backoff_seconds,
            result,
            reservation_token,
        )

    async def _record_skip(
        self, provider_id: str, credential_id: str, provider_model_id: str, reason: str
    ) -> None:
        await self._pool.execute(
            """insert into public.provider_events(
                 provider_id,credential_id,event_type,metadata)
               select $1,$2,'health_probe_skipped',jsonb_build_object(
                 'reason',$3::text,'provider_model_id',$4::text)
               where not exists (
                 select 1 from public.provider_events
                 where credential_id=$2 and event_type='health_probe_skipped'
                   and metadata->>'reason'=$3 and created_at >= now()-interval '1 hour'
               )""",
            provider_id,
            credential_id,
            reason,
            provider_model_id,
        )
        log_event(
            logger,
            logging.INFO,
            "health_probe_skipped",
            provider_id=provider_id,
            credential_id=credential_id,
            provider_model_id=provider_model_id,
            reason=reason,
        )
