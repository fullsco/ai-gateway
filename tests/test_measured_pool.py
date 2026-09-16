import asyncio
from unittest.mock import AsyncMock

import pytest

from gateway.configuration.measured_pool import MeasuredPool
from gateway.metrics import metrics


@pytest.mark.asyncio
async def test_query_acquisition_records_wait_and_releases_on_error():
    raw = AsyncMock()
    connection = raw.acquire.return_value
    connection.fetch.side_effect = ValueError("synthetic")
    before = metrics.histograms["db_pool_wait"].count
    pool = MeasuredPool(raw)
    with pytest.raises(ValueError):
        await pool.fetch("select synthetic")
    raw.release.assert_awaited_once_with(connection, timeout=2)
    assert metrics.histograms["db_pool_wait"].count == before + 1


@pytest.mark.asyncio
async def test_explicit_acquisition_supports_await_and_context_cancellation():
    raw = AsyncMock()
    pool = MeasuredPool(raw)
    connection = await pool.acquire(timeout=2)
    await pool.release(connection)
    raw.acquire.assert_awaited_with(timeout=2)
    with pytest.raises(asyncio.CancelledError):
        async with pool.acquire():
            raise asyncio.CancelledError
    assert raw.release.await_count == 2


@pytest.mark.asyncio
async def test_failed_acquisition_does_not_release_an_unowned_connection():
    raw = AsyncMock()
    raw.acquire.side_effect = TimeoutError
    pool = MeasuredPool(raw)
    with pytest.raises(TimeoutError):
        async with pool.acquire(timeout=.01):
            pass
    raw.release.assert_not_awaited()
