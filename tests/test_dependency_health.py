import asyncio
from contextlib import suppress
from time import monotonic
from unittest.mock import AsyncMock

import httpx
import pytest

from gateway import dependency_health
from gateway.app import create_app
from gateway.config import Settings
from gateway.configuration.measured_pool import MeasuredPool
from gateway.dependency_health import DependencyHealth
from tests.test_models_api import make_runtime

REQUIRED_FEATURES = [
    {"distributed_admission_enabled": True},
    {"durable_accounting_enabled": True},
    {"workflows_enabled": True},
    {"service_role": "workflow"},
]


def settings(**features):
    return Settings(
        _env_file=None, environment="production", database_url=None,
        health_probe_enabled=False, alert_monitor_enabled=False,
        credential_usage_poll_enabled=False, **features,
    )


def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test")


@pytest.mark.asyncio
@pytest.mark.parametrize("features", REQUIRED_FEATURES)
@pytest.mark.parametrize("method", ["GET", "HEAD"])
async def test_required_database_readiness_tracks_pool_health(features, method, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(dependency_health, "monotonic", lambda: clock[0])
    pool = AsyncMock()
    pool.fetchval.return_value = 1
    runtime, _ = make_runtime()
    app = create_app(settings(**features), runtime, db_pool=pool)
    # Exercise readiness independently of the stricter startup schema gates.
    app.state.ready = True
    async with client_for(app) as client:
        assert (await client.request(method, "/ready")).status_code == 200
        pool.fetchval.assert_awaited_once_with(
            "SELECT 1", timeout=dependency_health.PROBE_TIMEOUT_SECONDS,
        )
        pool.fetchval.side_effect = ConnectionError("private connection details")
        clock[0] += dependency_health.CACHE_SECONDS + .01
        failed = await client.request(method, "/ready")
        assert failed.status_code == 503
        if method == "GET":
            assert failed.json() == {"status": "not_ready"}
        assert (await client.get("/health")).status_code == 200
        pool.fetchval.side_effect = None
        clock[0] += dependency_health.CACHE_SECONDS + .01
        assert (await client.request(method, "/ready")).status_code == 200
        app.state.db_pool = None
        assert (await client.request(method, "/ready")).status_code == 503


@pytest.mark.asyncio
@pytest.mark.parametrize("features", REQUIRED_FEATURES)
async def test_required_feature_without_pool_is_not_ready(features):
    runtime, _ = make_runtime()
    app = create_app(settings(**features), runtime)
    app.state.ready = True
    async with client_for(app) as client:
        assert (await client.get("/ready")).status_code == 503


@pytest.mark.asyncio
@pytest.mark.parametrize("with_pool", [False, True])
async def test_static_runtime_without_database_features_remains_ready(with_pool):
    pool = AsyncMock() if with_pool else None
    if pool is not None:
        pool.fetchval.side_effect = ConnectionError("offline")
        pool.fetch.return_value = []
    runtime, _ = make_runtime()
    app = create_app(settings(), runtime, db_pool=pool)
    async with app.router.lifespan_context(app), client_for(app) as client:
        assert (await client.get("/ready")).status_code == 200
        if pool is not None:
            pool.fetchval.assert_not_awaited()
    assert not app.state.ready


@pytest.mark.asyncio
async def test_injected_asyncmock_pool_obeys_readiness_after_lifespan_startup(monkeypatch):
    pool = AsyncMock()
    pool.fetch.return_value = []
    pool.fetchval.return_value = 1
    app = create_app(settings(distributed_admission_enabled=True), make_runtime()[0], db_pool=pool)
    async with app.router.lifespan_context(app), client_for(app) as client:
        pool.fetchval.reset_mock()
        assert (await client.get("/ready")).status_code == 200
        pool.fetchval.assert_awaited_once_with(
            "SELECT 1", timeout=dependency_health.PROBE_TIMEOUT_SECONDS,
        )
        pool.fetchval.side_effect = ConnectionError()
        monkeypatch.setattr(dependency_health, "monotonic", lambda: float("inf"))
        assert (await client.get("/ready")).status_code == 503


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, 0, True, "1", AsyncMock()])
async def test_probe_requires_real_select_one_result(value):
    pool = AsyncMock()
    pool.fetchval.return_value = value
    assert not await DependencyHealth().healthy(pool)


@pytest.mark.asyncio
async def test_success_and_failure_cache_expire_and_concurrent_callers_share_probe(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(dependency_health, "monotonic", lambda: clock[0])
    release = asyncio.Event()
    healthy = True

    async def query(*args, **kwargs):
        await release.wait()
        if not healthy:
            raise ConnectionError()
        return 1

    pool = AsyncMock()
    pool.fetchval.side_effect = query
    checker = DependencyHealth()
    callers = [asyncio.create_task(checker.healthy(pool)) for _ in range(40)]
    release.set()
    assert all(await asyncio.gather(*callers))
    assert pool.fetchval.await_count == 1
    assert await checker.healthy(pool)
    assert pool.fetchval.await_count == 1
    healthy = False
    clock[0] += dependency_health.CACHE_SECONDS + .01
    assert not any(await asyncio.gather(*(checker.healthy(pool) for _ in range(40))))
    assert pool.fetchval.await_count == 2
    assert not await checker.healthy(pool)
    assert pool.fetchval.await_count == 2
    healthy = True
    clock[0] += dependency_health.CACHE_SECONDS + .01
    assert await checker.healthy(pool)
    assert pool.fetchval.await_count == 3


@pytest.mark.asyncio
async def test_cancelled_caller_does_not_cancel_shared_probe():
    entered, release = asyncio.Event(), asyncio.Event()

    async def query(*args, **kwargs):
        entered.set()
        await release.wait()
        return 1

    pool = AsyncMock()
    pool.fetchval.side_effect = query
    checker = DependencyHealth()
    first = asyncio.create_task(checker.healthy(pool))
    await entered.wait()
    second = asyncio.create_task(checker.healthy(pool))
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    assert await second
    pool.fetchval.assert_awaited_once()


@pytest.mark.asyncio
async def test_timeout_bounds_pool_wait_even_when_cancellation_cleanup_is_slow(monkeypatch):
    cancelled, release = asyncio.Event(), asyncio.Event()
    clock = [100.0]
    monkeypatch.setattr(dependency_health, "monotonic", lambda: clock[0])

    async def acquire(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
            raise

    raw_pool = AsyncMock()
    raw_pool.acquire.side_effect = acquire
    pool = MeasuredPool(raw_pool)
    app = create_app(settings(durable_accounting_enabled=True), make_runtime()[0], db_pool=pool)
    app.state.ready = True
    try:
        async with client_for(app) as client:
            start = monotonic()
            responses = await asyncio.wait_for(
                asyncio.gather(*(client.get("/ready") for _ in range(20))), timeout=1,
            )
            assert monotonic() - start < 1
            assert all(response.status_code == 503 for response in responses)
            await asyncio.wait_for(cancelled.wait(), .1)
            clock[0] += dependency_health.CACHE_SECONDS + .01
            assert (await client.get("/ready")).status_code == 503
            raw_pool.acquire.assert_awaited_once()
    finally:
        release.set()
        await app.state.dependency_health.close()


@pytest.mark.asyncio
async def test_pool_replacement_never_reuses_old_success():
    first, second = AsyncMock(), AsyncMock()
    first.fetchval.return_value = 1
    second.fetchval.side_effect = ConnectionError()
    checker = DependencyHealth()
    assert await checker.healthy(first)
    assert not await checker.healthy(second)
    second.fetchval.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["draining", "not_started", "pool_removed"])
async def test_runtime_and_drain_gates_short_circuit_and_are_rechecked_after_probe(state):
    entered, release = asyncio.Event(), asyncio.Event()

    async def query(*args, **kwargs):
        entered.set()
        await release.wait()
        return 1

    pool = AsyncMock()
    pool.fetchval.side_effect = query
    app = create_app(settings(distributed_admission_enabled=True), db_pool=pool)
    app.state.ready = state == "draining"
    app.state.drain.draining = state == "draining"
    async with client_for(app) as client:
        assert (await client.get("/ready")).status_code == 503
        pool.fetchval.assert_not_awaited()
        app.state.ready = True
        app.state.drain.draining = False
        response = asyncio.create_task(client.get("/ready"))
        await entered.wait()
        app.state.ready = state == "draining"
        app.state.drain.draining = state == "draining"
        if state == "pool_removed":
            app.state.ready = True
            app.state.db_pool = None
        release.set()
        assert (await response).status_code == 503


@pytest.mark.asyncio
async def test_close_cancels_pending_probe_and_query():
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def query(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    pool = AsyncMock()
    pool.fetchval.side_effect = query
    checker = DependencyHealth()
    pending = asyncio.create_task(checker.healthy(pool))
    await entered.wait()
    await checker.close()
    assert cancelled.is_set()
    with suppress(asyncio.CancelledError):
        assert not await pending
