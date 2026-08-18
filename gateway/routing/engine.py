from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

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


@dataclass(frozen=True)
class RouteDecision:
    canonical_model_id: str
    provider_model: ProviderModel
    credential: CredentialState
    score: float


class NoRouteAvailable(LookupError):
    pass


class RoutingEngine:
    def __init__(self, registry: ModelRegistry, policy: RoutingPolicy | None = None) -> None:
        self.registry = registry
        self.policy = policy or RoutingPolicy()

    def select(
        self,
        request: NormalizedRequest,
        providers: list[ProviderState],
        credentials: list[CredentialState],
        *,
        now: datetime | None = None,
        excluded_credential_ids: frozenset[str] = frozenset(),
        excluded_provider_model_ids: frozenset[str] = frozenset(),
    ) -> RouteDecision:
        current_time = now or datetime.now(UTC)
        provider_states = {provider.provider_id: provider for provider in providers}
        canonical_model = self.registry.resolve(request.requested_model)
        candidates: list[RouteDecision] = []

        for provider_model in self.registry.eligible_provider_models(request):
            if provider_model.id in excluded_provider_model_ids:
                continue
            provider = provider_states.get(provider_model.provider_id)
            if not self._provider_is_eligible(provider):
                continue
            for credential in credentials:
                if not self._credential_is_eligible(
                    credential,
                    provider_model,
                    provider,
                    current_time,
                    excluded_credential_ids,
                ):
                    continue
                score = self._score(provider_model, provider, credential)
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
        return min(
            candidates,
            key=lambda candidate: self._selection_key(candidate, provider_states),
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
    def _provider_is_eligible(provider: ProviderState | None) -> bool:
        return bool(
            provider
            and provider.enabled
            and not provider.circuit_open
            and provider.health in ROUTABLE_HEALTH
        )

    @staticmethod
    def _credential_is_eligible(
        credential: CredentialState,
        provider_model: ProviderModel,
        provider: ProviderState,
        now: datetime,
        excluded_ids: frozenset[str],
    ) -> bool:
        if credential.credential_id in excluded_ids:
            return False
        if not credential.enabled or credential.provider_id != provider_model.provider_id:
            return False
        if credential.health not in ROUTABLE_HEALTH:
            return False
        if credential.cooldown_until is not None and credential.cooldown_until > now:
            return False
        if (
            min(
                credential.quota_headroom,
                credential.rpm_headroom,
                credential.tpm_headroom,
                credential.concurrency_headroom,
            )
            <= 0
        ):
            return False
        restrictions = credential.supported_provider_model_ids
        if restrictions and provider_model.id not in restrictions:
            return False
        pool_credentials = provider_model.allowed_credential_ids
        if pool_credentials is not None and credential.credential_id not in pool_credentials:
            return False
        policy = provider_model.routing_policy or {}
        max_latency = policy.get("max_latency_ms")
        if max_latency is not None and max(
            provider.latency_ms, credential.latency_ms
        ) > float(max_latency):
            return False
        if credential.quota_headroom < float(policy.get("min_quota_headroom", 0)):
            return False
        if credential.rpm_headroom < float(policy.get("min_rpm_headroom", 0)):
            return False
        if credential.tpm_headroom < float(policy.get("min_tpm_headroom", 0)):
            return False
        allowed = policy.get("allowed_credential_ids") or []
        return not allowed or credential.credential_id in allowed

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
        member = (provider_model.pool_members or {}).get(credential.credential_id, {})
        member_weight = float(member.get("weight", 1)) if isinstance(member, dict) else 1
        return round(score * provider_model.weight * member_weight, 6)

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
        )

    @staticmethod
    def _bounded(value: float) -> float:
        return max(0, min(1, value))
