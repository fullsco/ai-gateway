import asyncio
from contextlib import suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from gateway import accounting_worker, maintenance
from gateway.app import create_app
from gateway.config import Settings


def settings(**overrides):
    return Settings(_env_file=None, database_url=None, health_probe_enabled=False,
                    alert_monitor_enabled=False, **overrides)


async def cancel(task):
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


def test_maintenance_defaults_are_opt_in_and_bounded():
    config = settings()
    assert config.maintenance_enabled is False
    assert config.maintenance_interval_seconds == 3600
    assert config.maintenance_timeout_seconds == 5
    assert config.telemetry_retention_days == 30


@pytest.mark.parametrize("name,values", [
    ("maintenance_interval_seconds", [0, 59, 86401, float("inf"), float("nan")]),
    ("maintenance_timeout_seconds", [0, .09, 61, float("inf"), float("nan")]),
    ("telemetry_retention_days", [6, 366, 7.5]),
])
def test_invalid_maintenance_bounds(name, values):
    for value in values:
        with pytest.raises(ValidationError):
            settings(**{name: value})


@pytest.mark.parametrize("value", [6, 366, True, 7.5, "30"])
@pytest.mark.asyncio
async def test_pruning_rejects_invalid_retention_before_queries(value):
    pool = AsyncMock()
    with pytest.raises(ValueError):
        await maintenance.prune_telemetry(pool, value)
    pool.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_only_allowlisted_telemetry_and_expired_artifacts_use_bounded_batches():
    pool = AsyncMock()
    pool.execute.return_value = "DELETE 100"
    assert await maintenance.prune_telemetry(pool, 45) == {
        "health_checks": "DELETE 100", "provider_events": "DELETE 100",
    }
    await maintenance.collect_expired_artifacts(pool)
    calls = pool.execute.await_args_list
    assert len(calls) == 3
    for call, table, timestamp in zip(calls, (
        "health_checks", "provider_events", "workflow_artifacts",
    ), ("checked_at", "created_at", "expires_at"), strict=True):
        sql = call.args[0]
        assert sql.startswith(f"delete from public.{table} where id in ")
        assert f"select id from public.{table}" in sql
        assert f"order by {timestamp} limit 100 for update skip locked" in sql
    assert calls[0].args[1:] == calls[1].args[1:] == (45,)
    assert "checked_at<now()-$1*interval '1 day'" in calls[0].args[0]
    assert "created_at<now()-$1*interval '1 day'" in calls[1].args[0]
    assert "where expires_at<=now()" in calls[2].args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_settings", [False, True])
async def test_default_accounting_expires_artifacts_but_never_prunes_telemetry(
    monkeypatch, explicit_settings,
):
    ledger = SimpleNamespace(recover_abandoned=AsyncMock(), reconcile=AsyncMock())
    monkeypatch.setattr(accounting_worker, "FinancialLedger", lambda pool: ledger)
    intervals = []
    finished, stopped = asyncio.Event(), asyncio.Event()

    async def sleep(interval):
        intervals.append(interval)
        if len(intervals) == 2:
            finished.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

    monkeypatch.setattr(maintenance, "asyncio", SimpleNamespace(
        timeout=asyncio.timeout, sleep=sleep,
    ))
    prune = AsyncMock()
    monkeypatch.setattr(maintenance, "prune_telemetry", prune)
    pool = AsyncMock()
    pool.fetchval.return_value = True
    task = asyncio.create_task(accounting_worker.accounting_loop(
        pool, **({"settings": settings()} if explicit_settings else {}),
    ))
    try:
        await asyncio.wait_for(finished.wait(), 1)
        assert ledger.reconcile.await_count >= 1
        assert intervals == [5, 5]
        assert pool.execute.await_count == 2
        for call in pool.execute.await_args_list:
            assert "delete from public.workflow_artifacts" in call.args[0]
            assert "where expires_at<=now()" in call.args[0]
        prune.assert_not_awaited()
    finally:
        await cancel(task)
    assert stopped.is_set()
    assert task.cancelled()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage,telemetry_enabled", [
    ("schema", False), ("artifacts", False),
    ("schema", True), ("artifacts", True), ("telemetry", True),
])
@pytest.mark.parametrize("failure", ["timeout", "error", "cancel"])
async def test_cleanup_is_off_reconciliation_path_and_cancellation_is_owned(
    monkeypatch, stage, telemetry_enabled, failure,
):
    started, stopped, reconciled, logged = (asyncio.Event() for _ in range(4))
    ledger = SimpleNamespace(recover_abandoned=AsyncMock(), reconcile=AsyncMock())

    async def reconcile(*, limit):
        assert limit == 100
        if started.is_set() and not stopped.is_set():
            reconciled.set()

    async def blocked(*args):
        started.set()
        try:
            await reconciled.wait()
            if failure == "error":
                raise RuntimeError("private database details")
            await asyncio.Event().wait()
        finally:
            stopped.set()

    def log(*args, **kwargs):
        assert args[2] == "maintenance_failed"
        assert kwargs == {"error_type": "TimeoutError" if failure == "timeout" else "RuntimeError"}
        logged.set()

    ledger.reconcile.side_effect = reconcile
    monkeypatch.setattr(accounting_worker, "FinancialLedger", lambda pool: ledger)
    monkeypatch.setattr(maintenance, "log_event", log)
    pool = AsyncMock()
    pool.fetchval.return_value = True
    monkeypatch.setattr(maintenance, "collect_expired_artifacts", AsyncMock())
    monkeypatch.setattr(maintenance, "prune_telemetry", AsyncMock())
    if stage == "schema":
        pool.fetchval.side_effect = blocked
    else:
        monkeypatch.setattr(maintenance, {
            "artifacts": "collect_expired_artifacts", "telemetry": "prune_telemetry",
        }[stage], blocked)
    task = asyncio.create_task(accounting_worker.accounting_loop(
        pool, interval=.001, settings=settings(
            maintenance_enabled=telemetry_enabled, maintenance_timeout_seconds=.1,
        ),
    ))
    try:
        await asyncio.wait_for(reconciled.wait(), 1)
        if failure != "cancel":
            await asyncio.wait_for(logged.wait(), 1)
            before = ledger.reconcile.await_count
            await asyncio.sleep(.01)
            assert ledger.reconcile.await_count > before
            if stage == "telemetry":
                assert maintenance.collect_expired_artifacts.await_count > 1
            elif telemetry_enabled:
                maintenance.prune_telemetry.assert_awaited_once()
        assert not task.done()
    finally:
        await asyncio.wait_for(cancel(task), 1)
    assert stopped.is_set()
    assert task.cancelled()
    assert logged.is_set() is (failure != "cancel")


@pytest.mark.asyncio
@pytest.mark.parametrize("artifacts_present", [False, True])
@pytest.mark.parametrize("fail_first_batch", [False, True])
async def test_schedule_sleeps_between_batches_and_retries_without_overlap(
    monkeypatch, artifacts_present, fail_first_batch,
):
    intervals = []
    finished = asyncio.Event()
    clock = [0]
    monkeypatch.setattr(maintenance, "monotonic", lambda: clock[0])

    async def sleep(interval):
        intervals.append(interval)
        clock[0] += interval
        if len(intervals) == 26:
            finished.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(maintenance, "asyncio", SimpleNamespace(
        timeout=asyncio.timeout, sleep=sleep,
    ))
    pool = AsyncMock()
    pool.fetchval.return_value = artifacts_present
    artifacts = AsyncMock()
    prune = AsyncMock(side_effect=[RuntimeError("failure"), None] if fail_first_batch else None)
    monkeypatch.setattr(maintenance, "collect_expired_artifacts", artifacts)
    monkeypatch.setattr(maintenance, "prune_telemetry", prune)
    task = asyncio.create_task(maintenance.maintenance_loop(pool, settings(
        maintenance_enabled=True, maintenance_interval_seconds=123, telemetry_retention_days=45,
    )))
    try:
        await asyncio.wait_for(finished.wait(), 1)
        assert intervals == [5] * 26
        assert prune.await_count == 2
        prune.assert_awaited_with(pool, 45)
        assert artifacts.await_count == (26 if artifacts_present else 0)
    finally:
        await cancel(task)


@pytest.mark.asyncio
@pytest.mark.parametrize("role,enabled,expected", [
    ("combined", True, True), ("maintenance", True, True),
    ("api", True, False), ("combined", False, False),
])
async def test_app_passes_settings_at_existing_accounting_placement(
    monkeypatch, role, enabled, expected,
):
    pool = AsyncMock()
    pool.fetchval.return_value = True
    pool.fetch.return_value = []
    loop = Mock(return_value=asyncio.sleep(0)) if expected else Mock()
    monkeypatch.setattr("gateway.app.accounting_loop", loop)
    monkeypatch.setattr("gateway.app.require_execution_schema", AsyncMock())
    config = settings(service_role=role, durable_accounting_enabled=enabled,
                      maintenance_enabled=True)
    app = create_app(config, db_pool=pool)
    async with app.router.lifespan_context(app):
        if expected:
            loop.assert_called_once_with(pool, settings=config)
        else:
            loop.assert_not_called()
