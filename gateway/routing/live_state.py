"""Dynamic operational state that overlays the published configuration.

The published snapshot defines what is *configured and allowed*: which providers,
credentials, mappings and routes exist, their priorities, weights, limits and pool
membership. It is deliberately immutable.

Operational reality changes far faster than configuration: a credential gets rate
limited, an upstream starts rejecting a key, latency degrades, a quota drains. That
state used to be frozen into the snapshot at publish time, so a failing credential
kept being selected until somebody published a new configuration.

This module keeps that state live. It is fed from two directions:

* immediately, in-process, from the outcome of every attempt (`record_attempt`);
* periodically from the database (`refresh`), so changes made by another worker or
  by an operator in the dashboard also take effect without a publish.

`overlay()` then rewrites the snapshot's ProviderState/CredentialState with current
values, which is all the routing engine consumes.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from gateway.logging import log_event
from gateway.providers import ErrorCategory
from gateway.routing.engine import CredentialState, HealthState, ProviderState

logger = logging.getLogger("gateway.routing.live_state")

# Health states that mean "do not route here right now".
_UNROUTABLE = {
    HealthState.AUTH_FAILED,
    HealthState.QUOTA_EXHAUSTED,
    HealthState.UNAVAILABLE,
    HealthState.DISABLED,
    HealthState.COOLDOWN,
}

_FAILURE_WINDOW_SECONDS = 300.0
# After a persisted non-routable health state has been in place this long, allow one
# half-open trial so the credential can prove it recovered. Without this a credential
# marked unavailable can never earn the success that would clear it.
_DB_HEALTH_RECOVERY_SECONDS = 60.0
_RATE_WINDOW_SECONDS = 60.0
_LATENCY_SMOOTHING = 0.3


class QuotaConfidence:
    """How much we actually know about a credential's remaining budget.

    Deliberately explicit: routing must never treat a guess as a measurement.
    """

    KNOWN = "known"        # operator limit + observed usage -> real headroom
    ESTIMATED = "estimated"  # usage observed but denominator unverified
    UNKNOWN = "unknown"    # no trustworthy signal at all


@dataclass
class _CredentialRuntime:
    """Per-credential mutable state held in this process."""

    failures: deque[tuple[float, str]] = field(default_factory=deque)
    successes: deque[float] = field(default_factory=deque)
    requests: deque[float] = field(default_factory=deque)
    tokens: deque[tuple[float, int]] = field(default_factory=deque)
    latency_ms: float | None = None
    # Set when an attempt fails in a way that should park the credential briefly.
    local_cooldown_until: float | None = None
    local_health: HealthState | None = None
    last_error_category: str | None = None
    # Populated from the database (operator config + upstream usage polling).
    quota_limit: float | None = None
    quota_used: float | None = None
    quota_confidence: str = QuotaConfidence.UNKNOWN
    db_health: HealthState | None = None
    db_unhealthy_since: float | None = None
    db_cooldown_until: datetime | None = None
    requests_per_minute: int | None = None
    tokens_per_minute: int | None = None
    enabled: bool = True

    def prune(self, now: float) -> None:
        while self.failures and now - self.failures[0][0] > _FAILURE_WINDOW_SECONDS:
            self.failures.popleft()
        while self.successes and now - self.successes[0] > _FAILURE_WINDOW_SECONDS:
            self.successes.popleft()
        while self.requests and now - self.requests[0] > _RATE_WINDOW_SECONDS:
            self.requests.popleft()
        while self.tokens and now - self.tokens[0][0] > _RATE_WINDOW_SECONDS:
            self.tokens.popleft()

    def failure_rate(self, now: float) -> float:
        self.prune(now)
        total = len(self.failures) + len(self.successes)
        if total == 0:
            return 0.0
        return len(self.failures) / total

    def consecutive_failures(self) -> int:
        """Failures since the most recent success."""
        if not self.failures:
            return 0
        last_success = self.successes[-1] if self.successes else 0.0
        return sum(1 for ts, _ in self.failures if ts > last_success)


@dataclass
class _ProviderRuntime:
    failures: deque[tuple[float, str]] = field(default_factory=deque)
    successes: deque[float] = field(default_factory=deque)
    latency_ms: float | None = None
    db_health: HealthState | None = None
    enabled: bool = True

    def prune(self, now: float) -> None:
        while self.failures and now - self.failures[0][0] > _FAILURE_WINDOW_SECONDS:
            self.failures.popleft()
        while self.successes and now - self.successes[0] > _FAILURE_WINDOW_SECONDS:
            self.successes.popleft()

    def failure_rate(self, now: float) -> float:
        self.prune(now)
        total = len(self.failures) + len(self.successes)
        if total == 0:
            return 0.0
        return len(self.failures) / total


@dataclass(frozen=True)
class QuotaPolicy:
    """Deprioritise, then park, a credential as its known budget drains.

    Only applied when the headroom is actually KNOWN. An unknown quota never makes
    a credential ineligible - we do not invent budget information.
    """

    soft_threshold: float = 0.15  # below this: deprioritise
    hard_threshold: float = 0.02  # below this: stop routing (recoverable)

    def __post_init__(self) -> None:
        if not 0 <= self.hard_threshold <= self.soft_threshold <= 1:
            raise ValueError("quota thresholds must satisfy 0 <= hard <= soft <= 1")


# How long a credential is parked locally after a failure, by category. These are
# routing states only: nothing is written to the credential's configuration.
_LOCAL_COOLDOWN_SECONDS: dict[str, float] = {
    ErrorCategory.UPSTREAM_AUTHENTICATION_ERROR.value: 120.0,
    ErrorCategory.RATE_LIMIT.value: 60.0,
    ErrorCategory.QUOTA_EXHAUSTED.value: 300.0,
    ErrorCategory.UPSTREAM_WAF_REJECTION.value: 30.0,
    ErrorCategory.PROVIDER_UNAVAILABLE.value: 15.0,
    ErrorCategory.TIMEOUT.value: 10.0,
}

# Categories that are conclusive about the credential on the very first failure.
# Everything else needs repeated failures before we park the credential, so a
# single upstream blip cannot take the last credential out of rotation.
_IMMEDIATE_COOLDOWN = {
    ErrorCategory.UPSTREAM_AUTHENTICATION_ERROR.value,
    ErrorCategory.RATE_LIMIT.value,
    ErrorCategory.QUOTA_EXHAUSTED.value,
}
_COOLDOWN_FAILURE_THRESHOLD = 3

_LOCAL_HEALTH: dict[str, HealthState] = {
    ErrorCategory.UPSTREAM_AUTHENTICATION_ERROR.value: HealthState.AUTH_FAILED,
    ErrorCategory.RATE_LIMIT.value: HealthState.RATE_LIMITED,
    ErrorCategory.QUOTA_EXHAUSTED.value: HealthState.QUOTA_EXHAUSTED,
    ErrorCategory.UPSTREAM_WAF_REJECTION.value: HealthState.DEGRADED,
    ErrorCategory.PROVIDER_UNAVAILABLE.value: HealthState.DEGRADED,
    ErrorCategory.TIMEOUT.value: HealthState.DEGRADED,
}


class LiveOperationalState:
    def __init__(
        self,
        pool: Any | None = None,
        *,
        quota_policy: QuotaPolicy | None = None,
        clock=time.monotonic,
    ) -> None:
        self._pool = pool
        self._quota_policy = quota_policy or QuotaPolicy()
        self._clock = clock
        self._credentials: dict[str, _CredentialRuntime] = {}
        self._providers: dict[str, _ProviderRuntime] = {}

    @property
    def quota_policy(self) -> QuotaPolicy:
        return self._quota_policy

    def _credential(self, credential_id: str) -> _CredentialRuntime:
        return self._credentials.setdefault(credential_id, _CredentialRuntime())

    def _provider(self, provider_id: str) -> _ProviderRuntime:
        return self._providers.setdefault(provider_id, _ProviderRuntime())

    # ------------------------------------------------------------------ ingest

    def record_attempt(
        self,
        *,
        provider_id: str,
        credential_id: str,
        succeeded: bool,
        error_category: str | None = None,
        latency_ms: float | None = None,
        retry_after_seconds: float | None = None,
        tokens: int | None = None,
        credential_at_fault: bool = True,
    ) -> None:
        """Fold one attempt outcome into live state, effective immediately."""
        now = self._clock()
        cred = self._credential(credential_id)
        prov = self._provider(provider_id)
        cred.requests.append(now)
        if tokens:
            cred.tokens.append((now, int(tokens)))
        if latency_ms is not None:
            cred.latency_ms = _smooth(cred.latency_ms, latency_ms)
            prov.latency_ms = _smooth(prov.latency_ms, latency_ms)

        if succeeded:
            cred.successes.append(now)
            prov.successes.append(now)
            # A success clears locally-inferred penalties straight away.
            cred.local_cooldown_until = None
            cred.local_health = None
            cred.last_error_category = None
            return

        category = error_category or ErrorCategory.INTERNAL_ERROR.value
        cred.last_error_category = category
        if not credential_at_fault:
            # Provider or edge level problem. Penalise the provider's score so it
            # becomes less preferred, but leave the credential usable - it may be
            # the only way to reach this model.
            prov.failures.append((now, category))
            return

        cred.failures.append((now, category))
        health = _LOCAL_HEALTH.get(category)
        if health is not None:
            cred.local_health = health
        # Park the credential only when the signal is conclusive, the upstream asked
        # us to back off, or it keeps failing.
        conclusive = category in _IMMEDIATE_COOLDOWN or retry_after_seconds is not None
        if conclusive or cred.consecutive_failures() >= _COOLDOWN_FAILURE_THRESHOLD:
            cooldown = retry_after_seconds or _LOCAL_COOLDOWN_SECONDS.get(category)
            if cooldown:
                cred.local_cooldown_until = now + float(cooldown)

    def record_tokens(self, credential_id: str, tokens: int) -> None:
        if tokens <= 0:
            return
        self._credential(credential_id).tokens.append((self._clock(), int(tokens)))

    # ----------------------------------------------------------------- refresh

    async def refresh(self) -> None:
        """Pull operator/worker-visible state from the database."""
        if self._pool is None:
            return
        try:
            creds = await self._pool.fetch(
                """select id::text, provider_id::text, enabled, health::text as health,
                          cooldown_until, quota_limit, quota_used,
                          requests_per_minute, tokens_per_minute,
                          quota_source::text as quota_source
                   from public.provider_credentials"""
            )
            provs = await self._pool.fetch(
                "select id::text, enabled, health::text as health from public.providers"
            )
        except Exception as exc:  # pragma: no cover - defensive
            log_event(
                logger,
                logging.WARNING,
                "live_state_refresh_failed",
                error_type=type(exc).__name__,
            )
            return

        try:
            for row in creds:
                data = dict(row)
                identifier = data.get("id")
                if identifier is None:
                    continue
                entry = self._credential(str(identifier))
                entry.enabled = bool(data.get("enabled", True))
                entry.db_health = _health(data.get("health"))
                if entry.db_health in _UNROUTABLE:
                    if entry.db_unhealthy_since is None:
                        entry.db_unhealthy_since = self._clock()
                else:
                    entry.db_unhealthy_since = None
                entry.db_cooldown_until = data.get("cooldown_until")
                entry.quota_limit = _float(data.get("quota_limit"))
                entry.quota_used = _float(data.get("quota_used"))
                entry.requests_per_minute = data.get("requests_per_minute")
                entry.tokens_per_minute = data.get("tokens_per_minute")
                source = data.get("quota_source") or "unknown"
                if entry.quota_limit not in (None, 0) and entry.quota_used is not None:
                    # A real denominator and a real numerator: headroom is measurable.
                    entry.quota_confidence = QuotaConfidence.KNOWN
                elif source == "upstream_usage" and entry.quota_used is not None:
                    # We know spend but not the ceiling - usable as a trend only.
                    entry.quota_confidence = QuotaConfidence.ESTIMATED
                else:
                    entry.quota_confidence = QuotaConfidence.UNKNOWN
            for row in provs:
                data = dict(row)
                identifier = data.get("id")
                if identifier is None:
                    continue
                entry_p = self._provider(str(identifier))
                entry_p.enabled = bool(data.get("enabled", True))
                entry_p.db_health = _health(data.get("health"))
        except Exception as exc:  # pragma: no cover - defensive
            log_event(
                logger,
                logging.WARNING,
                "live_state_refresh_unexpected_rows",
                error_type=type(exc).__name__,
            )

    # ----------------------------------------------------------------- overlay

    def overlay(
        self,
        providers: tuple[ProviderState, ...] | list[ProviderState],
        credentials: tuple[CredentialState, ...] | list[CredentialState],
        *,
        now: datetime | None = None,
    ) -> tuple[list[ProviderState], list[CredentialState], dict[str, dict[str, Any]]]:
        """Return snapshot states rewritten with live values, plus diagnostics."""
        wall = now or datetime.now(UTC)
        mono = self._clock()
        diagnostics: dict[str, dict[str, Any]] = {}

        new_providers: list[ProviderState] = []
        for provider in providers:
            runtime = self._providers.get(provider.provider_id)
            if runtime is None:
                new_providers.append(provider)
                continue
            health = runtime.db_health or provider.health
            new_providers.append(
                ProviderState(
                    provider_id=provider.provider_id,
                    enabled=provider.enabled and runtime.enabled,
                    health=health,
                    circuit_open=provider.circuit_open,
                    priority=provider.priority,
                    latency_ms=runtime.latency_ms
                    if runtime.latency_ms is not None
                    else provider.latency_ms,
                    failure_rate=max(provider.failure_rate, runtime.failure_rate(mono)),
                )
            )

        new_credentials: list[CredentialState] = []
        for credential in credentials:
            runtime = self._credentials.get(credential.credential_id)
            if runtime is None:
                new_credentials.append(credential)
                continue
            runtime.prune(mono)

            # A locally inferred penalty lasts only as long as its cooldown. Once it
            # lapses the credential is allowed back so it can prove itself; if it
            # fails again the penalty is simply re-applied. Without this a rate
            # limited credential could never recover, because it would never be
            # selected again to earn the success that clears it.
            if (
                runtime.local_cooldown_until is not None
                and runtime.local_cooldown_until <= mono
            ):
                runtime.local_cooldown_until = None
                runtime.local_health = None
            health = runtime.local_health or runtime.db_health or credential.health
            # Half-open recovery: a persisted unroutable state is downgraded to
            # DEGRADED after a grace period so this credential can be tried again.
            # DEGRADED still scores below healthy, so it is only chosen when nothing
            # better is available - and a success heals it for real.
            if (
                runtime.local_health is None
                and runtime.local_cooldown_until is None
                and health in _UNROUTABLE
                and runtime.db_unhealthy_since is not None
                and mono - runtime.db_unhealthy_since >= _DB_HEALTH_RECOVERY_SECONDS
            ):
                health = HealthState.DEGRADED
            cooldown = _latest_cooldown(
                credential.cooldown_until,
                runtime.db_cooldown_until,
                _mono_to_wall(runtime.local_cooldown_until, mono, wall),
            )

            quota_headroom, confidence = self._quota_headroom(runtime, credential)
            rpm_headroom = _rate_headroom(
                len(runtime.requests), runtime.requests_per_minute, credential.rpm_headroom
            )
            tpm_headroom = _rate_headroom(
                sum(count for _, count in runtime.tokens),
                runtime.tokens_per_minute,
                credential.tpm_headroom,
            )

            # A KNOWN quota below the hard threshold parks the credential. This is a
            # routing state: it disappears as soon as the quota recovers.
            if (
                confidence == QuotaConfidence.KNOWN
                and quota_headroom <= self._quota_policy.hard_threshold
            ):
                quota_headroom = 0.0

            new_credentials.append(
                CredentialState(
                    credential_id=credential.credential_id,
                    provider_id=credential.provider_id,
                    health=health,
                    enabled=credential.enabled and runtime.enabled,
                    priority=credential.priority,
                    quota_headroom=quota_headroom,
                    rpm_headroom=rpm_headroom,
                    tpm_headroom=tpm_headroom,
                    concurrency_headroom=credential.concurrency_headroom,
                    failure_rate=max(credential.failure_rate, runtime.failure_rate(mono)),
                    latency_ms=runtime.latency_ms
                    if runtime.latency_ms is not None
                    else credential.latency_ms,
                    cooldown_until=cooldown,
                    supported_provider_model_ids=credential.supported_provider_model_ids,
                    requests_per_minute=credential.requests_per_minute,
                    tokens_per_minute=credential.tokens_per_minute,
                )
            )
            diagnostics[credential.credential_id] = {
                "health": health.value if isinstance(health, HealthState) else str(health),
                "quota_headroom": round(quota_headroom, 4),
                "quota_confidence": confidence,
                "rpm_headroom": round(rpm_headroom, 4),
                "tpm_headroom": round(tpm_headroom, 4),
                "failure_rate": round(runtime.failure_rate(mono), 4),
                "consecutive_failures": runtime.consecutive_failures(),
                "recent_requests": len(runtime.requests),
                "last_error_category": runtime.last_error_category,
                "cooldown_until": cooldown.isoformat() if cooldown else None,
                "latency_ms": round(runtime.latency_ms, 1)
                if runtime.latency_ms is not None
                else None,
            }
        return new_providers, new_credentials, diagnostics

    def _quota_headroom(
        self, runtime: _CredentialRuntime, credential: CredentialState
    ) -> tuple[float, str]:
        if runtime.quota_limit in (None, 0) or runtime.quota_used is None:
            # No trustworthy budget signal: stay neutral rather than guess.
            return credential.quota_headroom, runtime.quota_confidence
        headroom = (runtime.quota_limit - runtime.quota_used) / runtime.quota_limit
        return max(0.0, min(1.0, headroom)), QuotaConfidence.KNOWN

    def quota_state(self, credential_id: str) -> dict[str, Any]:
        runtime = self._credentials.get(credential_id)
        if runtime is None:
            return {"confidence": QuotaConfidence.UNKNOWN}
        return {
            "confidence": runtime.quota_confidence,
            "limit": runtime.quota_limit,
            "used": runtime.quota_used,
        }


def _smooth(previous: float | None, sample: float) -> float:
    if previous is None:
        return sample
    return previous + _LATENCY_SMOOTHING * (sample - previous)


def _rate_headroom(observed: int, limit: int | None, fallback: float) -> float:
    if not limit or limit <= 0:
        return fallback
    return max(0.0, min(1.0, (limit - observed) / limit))


def _health(value: str | None) -> HealthState | None:
    if value is None:
        return None
    try:
        return HealthState(value)
    except ValueError:
        return None


def _float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _mono_to_wall(
    monotonic_deadline: float | None, mono_now: float, wall_now: datetime
) -> datetime | None:
    if monotonic_deadline is None:
        return None
    remaining = monotonic_deadline - mono_now
    if remaining <= 0:
        return None
    return wall_now + timedelta(seconds=remaining)


def _latest_cooldown(*values: datetime | None) -> datetime | None:
    present = [value for value in values if value is not None]
    if not present:
        return None
    return max(present)


__all__ = [
    "LiveOperationalState",
    "QuotaConfidence",
    "QuotaPolicy",
]
