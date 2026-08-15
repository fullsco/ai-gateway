from datetime import UTC, datetime, timedelta

from gateway.health import CircuitBreaker, CircuitPolicy, CircuitSnapshot, CircuitState

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_circuit_opens_after_failure_threshold() -> None:
    breaker = CircuitBreaker(CircuitPolicy(failure_threshold=2, cooldown_seconds=30))

    first = breaker.record_failure(CircuitSnapshot(), now=NOW)
    second = breaker.record_failure(first, now=NOW)

    assert first.state is CircuitState.CLOSED
    assert second.state is CircuitState.OPEN
    assert breaker.can_attempt(second, now=NOW + timedelta(seconds=29)) is None


def test_open_circuit_allows_single_probe_after_cooldown() -> None:
    breaker = CircuitBreaker(CircuitPolicy(cooldown_seconds=30))
    opened = CircuitSnapshot(state=CircuitState.OPEN, opened_at=NOW)

    probe = breaker.can_attempt(opened, now=NOW + timedelta(seconds=30))

    assert probe is not None
    assert probe.state is CircuitState.HALF_OPEN
    assert probe.probe_in_flight is True
    assert breaker.can_attempt(probe, now=NOW + timedelta(seconds=31)) is None


def test_half_open_circuit_closes_after_success_threshold() -> None:
    breaker = CircuitBreaker(CircuitPolicy(success_threshold=2))
    half_open = CircuitSnapshot(state=CircuitState.HALF_OPEN, probe_in_flight=True)

    first = breaker.record_success(half_open)
    second_probe = breaker.can_attempt(first, now=NOW)
    second = breaker.record_success(second_probe)

    assert first.state is CircuitState.HALF_OPEN
    assert second.state is CircuitState.CLOSED


def test_failed_probe_reopens_circuit() -> None:
    breaker = CircuitBreaker()
    half_open = CircuitSnapshot(state=CircuitState.HALF_OPEN, probe_in_flight=True)

    snapshot = breaker.record_failure(half_open, now=NOW)

    assert snapshot.state is CircuitState.OPEN
    assert snapshot.opened_at == NOW
