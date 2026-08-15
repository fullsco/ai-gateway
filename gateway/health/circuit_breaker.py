from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True)
class CircuitPolicy:
    failure_threshold: int = 3
    cooldown_seconds: float = 30
    success_threshold: int = 2

    def __post_init__(self) -> None:
        if self.failure_threshold < 1 or self.success_threshold < 1:
            raise ValueError("Circuit thresholds must be positive")
        if self.cooldown_seconds <= 0:
            raise ValueError("Circuit cooldown must be positive")


@dataclass(frozen=True)
class CircuitSnapshot:
    state: CircuitState = CircuitState.CLOSED
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    opened_at: datetime | None = None
    probe_in_flight: bool = False


class CircuitBreaker:
    def __init__(self, policy: CircuitPolicy | None = None) -> None:
        self.policy = policy or CircuitPolicy()

    def can_attempt(self, snapshot: CircuitSnapshot, *, now: datetime) -> CircuitSnapshot | None:
        if snapshot.state is CircuitState.CLOSED:
            return snapshot
        if snapshot.state is CircuitState.HALF_OPEN:
            if snapshot.probe_in_flight:
                return None
            return replace(snapshot, probe_in_flight=True)
        if snapshot.opened_at is None:
            return None
        cooldown = timedelta(seconds=self.policy.cooldown_seconds)
        if now < snapshot.opened_at + cooldown:
            return None
        return CircuitSnapshot(state=CircuitState.HALF_OPEN, probe_in_flight=True)

    def record_success(self, snapshot: CircuitSnapshot) -> CircuitSnapshot:
        if snapshot.state is CircuitState.CLOSED:
            return CircuitSnapshot()
        successes = snapshot.consecutive_successes + 1
        if successes >= self.policy.success_threshold:
            return CircuitSnapshot()
        return replace(
            snapshot,
            consecutive_failures=0,
            consecutive_successes=successes,
            probe_in_flight=False,
        )

    def record_failure(self, snapshot: CircuitSnapshot, *, now: datetime) -> CircuitSnapshot:
        if snapshot.state is CircuitState.HALF_OPEN:
            return CircuitSnapshot(
                state=CircuitState.OPEN,
                consecutive_failures=self.policy.failure_threshold,
                opened_at=now,
            )
        failures = snapshot.consecutive_failures + 1
        if failures >= self.policy.failure_threshold:
            return CircuitSnapshot(
                state=CircuitState.OPEN,
                consecutive_failures=failures,
                opened_at=now,
            )
        return replace(
            snapshot,
            consecutive_failures=failures,
            consecutive_successes=0,
            probe_in_flight=False,
        )
