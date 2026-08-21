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


def test_routing_trace_is_labelled_with_credential_names() -> None:
    """The trace identifies credentials by id, which an operator cannot read."""
    from gateway.admin.api import _label_trace, _trace_credential_ids

    trace = [
        {
            "attempt_number": 1,
            "selected": {"provider": "AgentRouter", "credential_id": "c-1"},
            "considered": [
                {"provider": "AgentRouter", "credential_id": "c-1", "eligible": True},
                {
                    "provider": "GoRouter",
                    "credential_id": "c-2",
                    "eligible": False,
                    "reason": "credential_health_auth_failed",
                },
            ],
        }
    ]

    assert _trace_credential_ids(trace) == {"c-1", "c-2"}

    labelled = _label_trace(trace, {"c-1": "primary", "c-2": "gorouter-key"})

    assert labelled[0]["selected"]["credential_name"] == "primary"
    assert [row["credential_name"] for row in labelled[0]["considered"]] == [
        "primary",
        "gorouter-key",
    ]
    # The reason is passed through untouched; the dashboard renders it.
    assert labelled[0]["considered"][1]["reason"] == "credential_health_auth_failed"


def test_routing_trace_survives_an_unknown_credential() -> None:
    from gateway.admin.api import _label_trace

    labelled = _label_trace(
        [{"considered": [{"provider": "P", "credential_id": "gone"}], "selected": None}], {}
    )

    assert labelled[0]["considered"][0]["credential_name"] == "Unavailable"


def test_routing_trace_decoding_tolerates_bad_input() -> None:
    from gateway.admin.api import _decode_trace

    assert _decode_trace(None) == []
    assert _decode_trace("not json") == []
    assert _decode_trace('{"not": "a list"}') == []
    assert _decode_trace('[{"attempt_number": 1}]') == [{"attempt_number": 1}]


def test_a_route_level_exclusion_reports_no_credential() -> None:
    """Excluding a whole route is not the same as failing to resolve a name."""
    from gateway.admin.api import _label_trace

    labelled = _label_trace(
        [{
            "considered": [
                {
                    "provider": "GoRouter",
                    "eligible": False,
                    "reason": "route_excluded_this_request",
                },
                {"provider": "AgentRouter", "credential_id": "c-1", "eligible": True},
            ],
            "selected": None,
        }],
        {"c-1": "primary"},
    )

    assert labelled[0]["considered"][0]["credential_name"] is None
    assert labelled[0]["considered"][1]["credential_name"] == "primary"
