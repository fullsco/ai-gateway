from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from gateway.providers import ErrorCategory, ProviderError
from gateway.routing.engine import RouteDecision


class AttemptOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class AttemptRecord:
    number: int
    provider_id: str
    credential_id: str
    started_at: datetime
    ended_at: datetime
    outcome: AttemptOutcome
    error_category: ErrorCategory | None = None
    upstream_status: int | None = None
    response_committed: bool = False


@dataclass(frozen=True)
class AttemptPolicy:
    max_attempts: int = 3

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be positive")


class AttemptCoordinator:
    def __init__(self, policy: AttemptPolicy | None = None) -> None:
        self.policy = policy or AttemptPolicy()

    def should_retry(
        self,
        *,
        error: ProviderError,
        attempts_made: int,
        response_committed: bool,
        now: datetime,
        deadline: datetime,
    ) -> bool:
        if response_committed or not error.retryable:
            return False
        if attempts_made >= self.policy.max_attempts:
            return False
        return now < deadline

    @staticmethod
    def excluded_credentials(attempts: list[AttemptRecord]) -> frozenset[str]:
        return frozenset(
            attempt.credential_id
            for attempt in attempts
            if attempt.outcome is AttemptOutcome.FAILED
        )

    @staticmethod
    def record_failure(
        *,
        number: int,
        route: RouteDecision,
        started_at: datetime,
        ended_at: datetime,
        error: ProviderError,
        response_committed: bool,
    ) -> AttemptRecord:
        return AttemptRecord(
            number=number,
            provider_id=route.provider_model.provider_id,
            credential_id=route.credential.credential_id,
            started_at=started_at,
            ended_at=ended_at,
            outcome=AttemptOutcome.FAILED,
            error_category=error.category,
            upstream_status=error.upstream_status,
            response_committed=response_committed,
        )
