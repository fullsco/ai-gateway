"""Bounded replay of durable settlement intent; unknown provider usage stays held."""

import asyncio
import logging
from contextlib import suppress

from gateway.accounting import FinancialLedger
from gateway.logging import log_event
from gateway.maintenance import maintenance_loop

logger = logging.getLogger("gateway.accounting")


async def accounting_loop(pool, *, interval=5, settings=None):
    ledger = FinancialLedger(pool)
    # Artifact expiry remains automatic, off the durable replay critical path.
    maintenance = asyncio.create_task(maintenance_loop(pool, settings, interval=interval))
    try:
        while True:
            try:
                await ledger.recover_abandoned()
                await ledger.reconcile(limit=100)
            except Exception as exc:
                log_event(logger, logging.ERROR, "settlement_reconciliation_failed",
                          error_type=type(exc).__name__)
            await asyncio.sleep(interval)
    finally:
        maintenance.cancel()
        with suppress(asyncio.CancelledError):
            await maintenance
