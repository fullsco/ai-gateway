from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from gateway.accounting import FinancialLedger
from gateway.admin.api import _authorize

router = APIRouter(prefix="/api/admin/v1/accounting", tags=["admin"])


class SettlementInput(BaseModel):
    actual_cost: Decimal = Field(ge=0, max_digits=18, decimal_places=8, allow_inf_nan=False)


@router.post("/{reservation_id}/settle")
async def reconcile_reservation(reservation_id: UUID, request: Request, body: SettlementInput):
    claims = await _authorize(request)
    if isinstance(claims, JSONResponse):
        return claims
    pool = request.app.state.db_pool
    if pool is None or not request.app.state.settings.durable_accounting_enabled:
        return JSONResponse({"error": "durable_accounting_unavailable"}, status_code=503)
    ledger = FinancialLedger(pool)
    try:
        await ledger.settle(reservation_id, actual=body.actual_cost)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    status = await pool.fetchval(
        "select status from public.financial_reservations where id=$1", reservation_id,
    )
    return {"id": str(reservation_id), "status": status}
