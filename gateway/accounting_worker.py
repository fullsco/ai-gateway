"""Bounded replay of durable settlement intent; unknown provider usage stays held."""

import asyncio
import logging

from gateway.accounting import FinancialLedger
from gateway.logging import log_event
from gateway.maintenance import collect_expired_artifacts

logger = logging.getLogger("gateway.accounting")


async def accounting_loop(pool, *, interval=5):
    ledger = FinancialLedger(pool)
    while True:
        try:
            await ledger.recover_abandoned()
            await ledger.reconcile(limit=100)
            if await pool.fetchval("select to_regclass('public.workflow_artifacts')"):
                await collect_expired_artifacts(pool)
        except Exception as exc:
            log_event(logger, logging.ERROR, "settlement_reconciliation_failed",
                      error_type=type(exc).__name__)
        await asyncio.sleep(interval)
