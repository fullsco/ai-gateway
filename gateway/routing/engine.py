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
            key=lambda candidate: (
                candidate.provider_model.priority,
                candidate.credential.priority,
                -candidate.score,
                candidate.provider_model.id,
                candidate.credential.credential_id,
            ),
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
        return not restrictions or provider_model.id in restrictions

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
        return round(score * provider_model.weight, 6)

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
