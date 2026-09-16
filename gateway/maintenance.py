"""Bounded retention for operational data, preserving unresolved financial evidence."""


async def collect_expired_artifacts(pool):
    return await pool.execute(
        "delete from public.workflow_artifacts where id in "
        "(select id from public.workflow_artifacts where expires_at<=now() "
        "order by expires_at limit 100 for update skip locked)"
    )


async def prune_telemetry(pool, retention_days=30):
    if type(retention_days) is not int or not 7 <= retention_days <= 365:
        raise ValueError("Telemetry retention must be 7–365 days")
    results = {}
    for table, timestamp in (("health_checks", "checked_at"), ("provider_events", "created_at")):
        results[table] = await pool.execute(
            f"delete from public.{table} where id in "
            f"(select id from public.{table} where {timestamp}<now()-$1*interval '1 day' "
            f"order by {timestamp} limit 100 for update skip locked)", retention_days,
        )
    return results
