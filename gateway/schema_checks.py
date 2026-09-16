"""Fail readiness before accepting work if required additive schema is missing."""


async def require_execution_schema(pool):
    expected = {
        "financial_reservations": {"workflow_owner", "workflow_version", "telemetry_request_id",
                                   "estimated_tokens", "actual_tokens"},
        "settlement_outbox": {"next_attempt_at", "attempts"},
        "workflow_jobs": {"reserved_tokens", "reserved_cost"},
    }
    rows = await pool.fetch(
        "select table_name,column_name from information_schema.columns "
        "where table_schema='public' and table_name=any($1::text[])", list(expected),
    )
    actual = {}
    for row in rows:
        actual.setdefault(row["table_name"], set()).add(row["column_name"])
    if any(not fields <= actual.get(table, set()) for table, fields in expected.items()):
        raise RuntimeError("Execution schema migration 032 or later is required")


async def require_circuit_schema(pool):
    exists = await pool.fetchval(
        "select exists(select 1 from information_schema.columns where table_schema='public' "
        "and table_name='route_circuits' and column_name='generation')"
    )
    if not exists:
        raise RuntimeError("Shared circuit migration 032 or later is required")
    versioned = await pool.fetchval(
        "select exists(select 1 from information_schema.columns where table_schema='public' "
        "and table_name='capacity_scopes' and column_name='version')"
    )
    if not versioned:
        raise RuntimeError("Capacity policy migration 033 or later is required")
