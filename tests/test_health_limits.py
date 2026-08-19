import pytest

from gateway.health.limits import HealthProbeLimiter, ProbeLimitConfig


class Pool:
    """Fake pool distinguishing the passive-signal check from the reservation."""

    def __init__(
        self, reservation: str = "reserved:test-token", *, recent_passive: bool = False
    ) -> None:
        self.reservation = reservation
        self.recent_passive = recent_passive
        self.fetchval_calls = []
        self.execute_calls = []

    async def fetchval(self, query, *args):
        self.fetchval_calls.append((query, args))
        if "last_used_at" in query:
            return 1 if self.recent_passive else None
        return self.reservation

    async def execute(self, query, *args):
        self.execute_calls.append((query, args))


@pytest.mark.asyncio
async def test_limiter_reserves_with_configured_daily_and_cooldown_limits():
    pool = Pool()
    limiter = HealthProbeLimiter(
        pool,
        ProbeLimitConfig(
            daily_limit=10,
            min_interval_seconds=7200,
            lease_seconds=30,
            failure_backoff_seconds=7200,
            max_backoff_seconds=86400,
        ),
    )

    result = await limiter.reserve("provider", "credential", "mapping")

    assert result == "reserved:test-token"
    reservation = next(
        call for call in pool.fetchval_calls if "reserve_health_probe" in call[0]
    )
    assert reservation[1] == (
        "provider",
        "credential",
        "mapping",
        10,
        7200,
        30,
        False,
        20,
        60,
    )
    assert pool.execute_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["in_progress", "cooldown", "daily_limit"])
async def test_limiter_records_bounded_skip_reasons(reason):
    pool = Pool(reason)
    limiter = HealthProbeLimiter(pool, ProbeLimitConfig())

    result = await limiter.reserve("provider", "credential", "mapping")

    assert result == reason
    assert len(pool.execute_calls) == 1
    assert "health_probe_skipped" in pool.execute_calls[0][0]
    assert pool.execute_calls[0][1][2] == reason


@pytest.mark.asyncio
async def test_limiter_manual_reservation_is_explicit_and_completion_is_bounded():
    pool = Pool()
    limiter = HealthProbeLimiter(pool, ProbeLimitConfig())

    await limiter.reserve("provider", "credential", "mapping", manual=True)
    await limiter.complete(
        "credential", "test-token", success=False, result="timeout"
    )

    assert pool.fetchval_calls[0][1][-3:] == (True, 20, 60)
    completion = pool.execute_calls[0][1]
    assert completion == (
        "credential", False, 7200, 7200, 86400, "timeout", "test-token"
    )


def test_limiter_rotates_across_routes_without_increasing_request_count(monkeypatch):
    pool = Pool()
    limiter = HealthProbeLimiter(pool, ProbeLimitConfig(min_interval_seconds=7200))
    monkeypatch.setattr("gateway.health.limits.time.time", lambda: 0)
    first = limiter.route_index("credential", 3)
    monkeypatch.setattr("gateway.health.limits.time.time", lambda: 7200)
    second = limiter.route_index("credential", 3)

    assert second == (first + 1) % 3


@pytest.mark.asyncio
async def test_recent_real_traffic_skips_the_probe_entirely():
    """Anthropic-protocol probes are billable, so do not pay for what traffic shows.

    A credential exercised by real requests within the window already has a
    passive health signal, making an active probe pure cost with no new
    information.
    """
    pool = Pool(recent_passive=True)
    limiter = HealthProbeLimiter(pool, ProbeLimitConfig(passive_signal_window_seconds=7200))

    result = await limiter.reserve("provider", "credential", "mapping")

    assert result == "recent_passive_signal"
    assert not any("reserve_health_probe" in call[0] for call in pool.fetchval_calls), (
        "no probe reservation may be taken when traffic already covers the credential"
    )
    assert pool.execute_calls[0][1][2] == "recent_passive_signal"


@pytest.mark.asyncio
async def test_manual_probe_ignores_the_passive_signal_window():
    """An operator asking for a probe explicitly must always get one."""
    pool = Pool(recent_passive=True)
    limiter = HealthProbeLimiter(pool, ProbeLimitConfig())

    result = await limiter.reserve("provider", "credential", "mapping", manual=True)

    assert result == "reserved:test-token"
    assert any("reserve_health_probe" in call[0] for call in pool.fetchval_calls)


@pytest.mark.asyncio
async def test_idle_credential_is_still_probed():
    pool = Pool(recent_passive=False)
    limiter = HealthProbeLimiter(pool, ProbeLimitConfig())

    result = await limiter.reserve("provider", "credential", "mapping")

    assert result == "reserved:test-token"
