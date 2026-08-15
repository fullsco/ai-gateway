from datetime import UTC, datetime, timedelta

import pytest

from gateway.models import ProviderModel
from gateway.protocols import ClientProtocol
from gateway.providers import ErrorCategory, ProviderError
from gateway.routing import (
    AttemptCoordinator,
    AttemptOutcome,
    AttemptPolicy,
    CredentialState,
    RouteDecision,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def make_error(*, retryable: bool = True) -> ProviderError:
    return ProviderError(
        category=ErrorCategory.RATE_LIMIT,
        message="Rate limited",
        retryable=retryable,
        upstream_status=429,
    )


def make_route() -> RouteDecision:
    return RouteDecision(
        canonical_model_id="model-x",
        provider_model=ProviderModel(
            id="provider-model",
            canonical_model_id="model-x",
            provider_id="provider-a",
            upstream_model_id="upstream-model",
            protocol=ClientProtocol.ANTHROPIC_MESSAGES,
            capabilities=frozenset(),
        ),
        credential=CredentialState("credential-a", "provider-a"),
        score=1,
    )


@pytest.mark.parametrize(
    ("retryable", "attempts", "committed", "deadline_delta", "expected"),
    [
        (True, 1, False, 10, True),
        (False, 1, False, 10, False),
        (True, 1, True, 10, False),
        (True, 3, False, 10, False),
        (True, 1, False, 0, False),
    ],
)
def test_retry_decision_respects_safety_boundaries(
    retryable, attempts, committed, deadline_delta, expected
) -> None:
    coordinator = AttemptCoordinator(AttemptPolicy(max_attempts=3))

    result = coordinator.should_retry(
        error=make_error(retryable=retryable),
        attempts_made=attempts,
        response_committed=committed,
        now=NOW,
        deadline=NOW + timedelta(seconds=deadline_delta),
    )

    assert result is expected


def test_failure_record_drives_key_rotation() -> None:
    coordinator = AttemptCoordinator()
    record = coordinator.record_failure(
        number=1,
        route=make_route(),
        started_at=NOW,
        ended_at=NOW + timedelta(seconds=1),
        error=make_error(),
        response_committed=False,
    )

    assert record.outcome is AttemptOutcome.FAILED
    assert record.provider_id == "provider-a"
    assert record.error_category is ErrorCategory.RATE_LIMIT
    assert coordinator.excluded_credentials([record]) == frozenset({"credential-a"})
