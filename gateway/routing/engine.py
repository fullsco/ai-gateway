import random
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from gateway.models import ModelRegistry, ProviderModel
from gateway.protocols import NormalizedRequest


class HealthState(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    RATE_LIMITED = "rate_limited"
    AUTH_FAILED = "auth_failed"
    QUOTA_EXHAUSTED = "quota_exhausted"
    UNAVAILABLE = "unavailable"
    COOLDOWN = "cooldown"
    DISABLED = "disabled"


ROUTABLE_HEALTH = {HealthState.HEALTHY, HealthState.DEGRADED}

# Scores within this fraction of the best are treated as equally good and load balanced.
_TIE_TOLERANCE = 0.05


@dataclass(frozen=True)
class ProviderState:
    provider_id: str
    health: HealthState = HealthState.HEALTHY
    enabled: bool = True
    circuit_open: bool = False
    latency_ms: float = 0
    failure_rate: float = 0
    priority: int = 100


@dataclass(frozen=True)
class CredentialState:
    credential_id: str
    provider_id: str
    health: HealthState = HealthState.HEALTHY
    enabled: bool = True
    priority: int = 100
    quota_headroom: float = 1
    rpm_headroom: float = 1
    tpm_headroom: float = 1
    concurrency_headroom: float = 1
    failure_rate: float = 0
    latency_ms: float = 0
    cooldown_until: datetime | None = None
    supported_provider_model_ids: frozenset[str] = frozenset()
    requests_per_minute: int | None = None
    tokens_per_minute: int | None = None


@dataclass(frozen=True)
class RoutingPolicy:
    health_weight: float = 3
    quota_weight: float = 2
    rate_limit_weight: float = 2
    concurrency_weight: float = 1
    latency_weight: float = 1
    failure_weight: float = 2
    cost_weight: float = 0
    # Below this KNOWN quota headroom a credential is deprioritised but stays
    # eligible, so traffic drains away before it is actually exhausted.
    quota_soft_threshold: float = 0.15
    quota_soft_penalty: float = 0.5
    # Spread traffic across equally-ranked candidates instead of pinning one.
    distribute_within_tier: bool = True


@dataclass
class RoutingTrace:
    """Explains a routing decision: what was considered, excluded, and why."""

    requested_model: str | None = None
    canonical_model_id: str | None = None
    strategy: str | None = None
    considered: list[dict[str, Any]] = field(default_factory=list)
    selected: dict[str, Any] | None = None
    attempt_number: int = 1
    is_fallback: bool = False
    fallback_reason: str | None = None

    def add(self, entry: dict[str, Any]) -> None:
        self.considered.append(entry)

    @property
    def excluded(self) -> list[dict[str, Any]]:
        return [item for item in self.considered if not item.get("eligible")]

    def as_dict(self) -> dict[str, Any]:
        return {
            "requested_model": self.requested_model,
            "canonical_model_id": self.canonical_model_id,
            "strategy": self.strategy,
            "attempt_number": self.attempt_number,
            "is_fallback": self.is_fallback,
            "fallback_reason": self.fallback_reason,
            "selected": self.selected,
            "candidate_count": len(self.considered),
            "eligible_count": sum(1 for c in self.considered if c.get("eligible")),
            "considered": self.considered,
        }


@dataclass(frozen=True)
class RouteDecision:
    canonical_model_id: str
    provider_model: ProviderModel
    credential: CredentialState
    score: float


class NoRouteAvailable(LookupError):
    pass


class RoutingEngine:
    def __init__(
        self, registry: ModelRegistry, policy: RoutingPolicy | None = None, rng=None
    ) -> None:
        self.registry = registry
        self.policy = policy or RoutingPolicy()
        self._rng = rng or random.Random()

    def select(
        self,
        request: NormalizedRequest,
        providers: list[ProviderState],
        credentials: list[CredentialState],
        *,
        now: datetime | None = None,
        excluded_credential_ids: frozenset[str] = frozenset(),
        excluded_provider_model_ids: frozenset[str] = frozenset(),
        trace: RoutingTrace | None = None,
        diagnostics: dict[str, dict[str, Any]] | None = None,
    ) -> RouteDecision:
        current_time = now or datetime.now(UTC)
        provider_states = {provider.provider_id: provider for provider in providers}
        canonical_model = self.registry.resolve(request.requested_model)
        candidates: list[RouteDecision] = []

        if trace is not None:
            trace.requested_model = request.requested_model
            trace.canonical_model_id = canonical_model.id

        for provider_model in self.registry.eligible_provider_models(request):
            provider = provider_states.get(provider_model.provider_id)
            if trace is not None:
                trace.strategy = provider_model.pool_strategy or "priority"

            reason: str | None = None
            if provider_model.id in excluded_provider_model_ids:
                reason = "route_excluded_this_request"
            else:
                reason = self._provider_exclusion_reason(provider)

            if reason is not None:
                if trace is not None:
                    trace.add(
                        {
                            "provider_id": provider_model.provider_id,
                            "provider": provider_model.provider_name,
                            "provider_model_id": provider_model.id,
                            "credential_id": None,
                            "eligible": False,
                            "reason": reason,
                        }
                    )
                continue

            assert provider is not None
            for credential in credentials:
                if credential.provider_id != provider_model.provider_id:
                    continue
                reason = self._credential_exclusion_reason(
                    credential,
                    provider_model,
                    provider,
                    current_time,
                    excluded_credential_ids,
                )
                entry: dict[str, Any] = {
                    "provider_id": provider_model.provider_id,
                    "provider": provider_model.provider_name,
                    "provider_model_id": provider_model.id,
                    "credential_id": credential.credential_id,
                    "eligible": reason is None,
                    "reason": reason,
                    "route_priority": provider_model.priority,
                    "provider_priority": provider.priority,
                    "credential_priority": credential.priority,
                }
                if diagnostics and credential.credential_id in diagnostics:
                    entry["live"] = diagnostics[credential.credential_id]
                if reason is not None:
                    if trace is not None:
                        trace.add(entry)
                    continue
                score = self._score(provider_model, provider, credential)
                entry["score"] = score
                if trace is not None:
                    trace.add(entry)
                candidates.append(
                    RouteDecision(
                        canonical_model_id=canonical_model.id,
                        provider_model=provider_model,
                        credential=credential,
                        score=score,
                    )
                )

        if not candidates:
            raise NoRouteAvailable(f"No eligible route for model {canonical_model.id}")

        chosen = self._choose(candidates, provider_states)
        if trace is not None:
            trace.selected = {
                "provider_id": chosen.provider_model.provider_id,
                "provider": chosen.provider_model.provider_name,
                "provider_model_id": chosen.provider_model.id,
                "credential_id": chosen.credential.credential_id,
                "score": chosen.score,
                "upstream_model_id": chosen.provider_model.upstream_model_id,
            }
        return chosen

    def _choose(
        self,
        candidates: list[RouteDecision],
        provider_states: dict[str, ProviderState],
    ) -> RouteDecision:
        """Pick the best candidate, spreading load across equally-ranked ones.

        Explicit operator intent (route priority, provider priority, pool member
        priority) is a hard ordering and is never traded away. Only candidates that
        tie on all of it are load balanced, weighted by their score.
        """
        ordered = sorted(
            candidates, key=lambda candidate: self._selection_key(candidate, provider_states)
        )
        best = ordered[0]
        strategy = best.provider_model.pool_strategy or "priority"
        policy = self._policy_for(best.provider_model)
        if not policy.distribute_within_tier or strategy == "least_loaded":
            # least_loaded is an explicit instruction to pick the emptiest target.
            return best

        tier_key = self._tier_key(best, provider_states)
        tier = [
            candidate
            for candidate in ordered
            if self._tier_key(candidate, provider_states) == tier_key
        ]
        if len(tier) == 1:
            return best

        if strategy != "weighted":
            # Only spread across candidates that are genuinely as good as the best
            # one, so a healthier or faster credential still wins outright.
            cutoff = best.score * (1 - _TIE_TOLERANCE) if best.score > 0 else best.score
            tier = [candidate for candidate in tier if candidate.score >= cutoff]
            if len(tier) == 1:
                return best

        weights = [max(candidate.score, 1e-6) for candidate in tier]
        return self._rng.choices(tier, weights=weights, k=1)[0]

    def _tier_key(
        self, candidate: RouteDecision, provider_states: dict[str, ProviderState]
    ) -> tuple[object, ...]:
        """The part of the ordering that encodes explicit operator intent."""
        return (
            candidate.provider_model.priority,
            provider_states[candidate.provider_model.provider_id].priority,
            self._member_priority(candidate),
        )

    def _selection_key(
        self,
        candidate: RouteDecision,
        provider_states: dict[str, ProviderState],
    ) -> tuple[object, ...]:
        strategy = candidate.provider_model.pool_strategy
        if strategy == "least_loaded":
            return (
                -min(
                    candidate.credential.concurrency_headroom,
                    candidate.credential.rpm_headroom,
                    candidate.credential.tpm_headroom,
                ),
                candidate.provider_model.priority,
                provider_states[candidate.provider_model.provider_id].priority,
                self._member_priority(candidate),
                -candidate.score,
                candidate.provider_model.id,
                candidate.credential.credential_id,
            )
        if strategy == "weighted":
            return (
                candidate.provider_model.priority,
                provider_states[candidate.provider_model.provider_id].priority,
                -candidate.score,
                candidate.provider_model.id,
                candidate.credential.credential_id,
            )
        return (
            candidate.provider_model.priority,
            provider_states[candidate.provider_model.provider_id].priority,
            self._member_priority(candidate),
            -candidate.score,
            candidate.provider_model.id,
            candidate.credential.credential_id,
        )

    @staticmethod
    def _provider_exclusion_reason(provider: ProviderState | None) -> str | None:
        if provider is None:
            return "provider_missing_from_snapshot"
        if not provider.enabled:
            return "provider_disabled"
        if provider.circuit_open:
            return "provider_circuit_open"
        if provider.health not in ROUTABLE_HEALTH:
            return f"provider_health_{provider.health.value}"
        return None

    @staticmethod
    def _credential_exclusion_reason(
        credential: CredentialState,
        provider_model: ProviderModel,
        provider: ProviderState,
        now: datetime,
        excluded_ids: frozenset[str],
    ) -> str | None:
        if credential.credential_id in excluded_ids:
            return "credential_excluded_this_request"
        if not credential.enabled:
            return "credential_disabled"
        if credential.provider_id != provider_model.provider_id:
            return "credential_other_provider"
        if credential.health not in ROUTABLE_HEALTH:
            return f"credential_health_{credential.health.value}"
        if credential.cooldown_until is not None and credential.cooldown_until > now:
            return "credential_in_cooldown"
        if credential.quota_headroom <= 0:
            return "credential_quota_exhausted"
        if credential.rpm_headroom <= 0:
            return "credential_rpm_exhausted"
        if credential.tpm_headroom <= 0:
            return "credential_tpm_exhausted"
        if credential.concurrency_headroom <= 0:
            return "credential_concurrency_exhausted"
        restrictions = credential.supported_provider_model_ids
        if restrictions and provider_model.id not in restrictions:
            return "credential_not_permitted_for_route"
        pool_credentials = provider_model.allowed_credential_ids
        if pool_credentials is not None and credential.credential_id not in pool_credentials:
            return "credential_not_in_route_pool"
        policy = provider_model.routing_policy or {}
        max_latency = policy.get("max_latency_ms")
        if max_latency is not None and max(
            provider.latency_ms, credential.latency_ms
        ) > float(max_latency):
            return "latency_above_policy_limit"
        if credential.quota_headroom < float(policy.get("min_quota_headroom", 0)):
            return "quota_headroom_below_policy_minimum"
        if credential.rpm_headroom < float(policy.get("min_rpm_headroom", 0)):
            return "rpm_headroom_below_policy_minimum"
        if credential.tpm_headroom < float(policy.get("min_tpm_headroom", 0)):
            return "tpm_headroom_below_policy_minimum"
        allowed = policy.get("allowed_credential_ids") or []
        if allowed and credential.credential_id not in allowed:
            return "credential_not_in_policy_allow_list"
        return None

    def _score(
        self,
        provider_model: ProviderModel,
        provider: ProviderState,
        credential: CredentialState,
    ) -> float:
        health = 1 if credential.health is HealthState.HEALTHY else 0.5
        rate_headroom = min(credential.rpm_headroom, credential.tpm_headroom)
        latency = 1 / (1 + max(provider.latency_ms, credential.latency_ms) / 1000)
        failure_rate = max(provider.failure_rate, credential.failure_rate)
        policy = self._policy_for(provider_model)
        score = (
            policy.health_weight * health
            + policy.quota_weight * self._bounded(credential.quota_headroom)
            + policy.rate_limit_weight * self._bounded(rate_headroom)
            + policy.concurrency_weight * self._bounded(credential.concurrency_headroom)
            + policy.latency_weight * latency
            - policy.failure_weight * self._bounded(failure_rate)
        )
        cost = self._cost_signal(provider_model)
        if cost is not None:
            # Cheaper is better; only applied when pricing is actually configured.
            score -= policy.cost_weight * cost
        member = (provider_model.pool_members or {}).get(credential.credential_id, {})
        member_weight = float(member.get("weight", 1)) if isinstance(member, dict) else 1
        score = score * provider_model.weight * member_weight
        # Drain traffic away from a credential whose KNOWN budget is nearly gone,
        # before it hard-fails. Eligibility is unchanged; only preference drops.
        if 0 < credential.quota_headroom < policy.quota_soft_threshold:
            score *= policy.quota_soft_penalty
        return round(score, 6)

    @staticmethod
    def _cost_signal(provider_model: ProviderModel) -> float | None:
        """Normalised 0..1 cost, or None when pricing is not configured.

        We never invent pricing: an empty pricing map yields no cost influence.
        """
        pricing = provider_model.pricing or {}
        if not isinstance(pricing, dict) or not pricing:
            return None
        for key in ("normalized", "score", "input_per_mtok", "input_cost_per_mtok"):
            value = pricing.get(key)
            if isinstance(value, (int, float)) and value >= 0:
                return max(0.0, min(1.0, float(value) / 100.0))
        return None

    @staticmethod
    def _member_priority(candidate: RouteDecision) -> int:
        member = (candidate.provider_model.pool_members or {}).get(
            candidate.credential.credential_id, {}
        )
        if isinstance(member, dict):
            return int(member.get("priority", candidate.credential.priority))
        return candidate.credential.priority

    def _policy_for(self, provider_model: ProviderModel) -> RoutingPolicy:
        overrides = provider_model.routing_policy or {}
        return RoutingPolicy(
            health_weight=overrides.get("health_weight", self.policy.health_weight),
            quota_weight=overrides.get("quota_weight", self.policy.quota_weight),
            rate_limit_weight=overrides.get(
                "rate_limit_weight", self.policy.rate_limit_weight
            ),
            concurrency_weight=overrides.get(
                "concurrency_weight", self.policy.concurrency_weight
            ),
            latency_weight=overrides.get("latency_weight", self.policy.latency_weight),
            failure_weight=overrides.get("failure_weight", self.policy.failure_weight),
            cost_weight=overrides.get("cost_weight", self.policy.cost_weight),
            quota_soft_threshold=overrides.get(
                "quota_soft_threshold", self.policy.quota_soft_threshold
            ),
            quota_soft_penalty=overrides.get(
                "quota_soft_penalty", self.policy.quota_soft_penalty
            ),
            distribute_within_tier=overrides.get(
                "distribute_within_tier", self.policy.distribute_within_tier
            ),
        )

    @staticmethod
    def _bounded(value: float) -> float:
        return max(0, min(1, value))
