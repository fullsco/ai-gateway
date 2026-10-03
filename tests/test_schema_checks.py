from unittest.mock import AsyncMock

import pytest

from gateway.schema_checks import require_circuit_schema, require_execution_schema


@pytest.mark.asyncio
async def test_existing_tables_without_required_columns_fail_startup_gate():
    pool = AsyncMock()
    pool.fetch.return_value = [{"table_name": "financial_reservations", "column_name": "id"}]
    with pytest.raises(RuntimeError, match="032"):
        await require_execution_schema(pool)
    pool.fetchval.return_value = False
    with pytest.raises(RuntimeError, match="032"):
        await require_circuit_schema(pool)


@pytest.mark.asyncio
async def test_circuit_generation_without_capacity_version_rejects_schema_032():
    pool = AsyncMock()
    pool.fetchval.side_effect = [True, False]
    with pytest.raises(RuntimeError, match="033"):
        await require_circuit_schema(pool)
    pool.fetchval.side_effect = [True, True]
    await require_circuit_schema(pool)
