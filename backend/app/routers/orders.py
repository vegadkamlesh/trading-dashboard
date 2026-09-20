"""Order endpoints — preview, place (super order), cancel, and book views.

Safety:
  * session required,
  * order PIN required for anything that can move money,
  * DRY-RUN mode (default) validates + audits but does NOT hit Dhan.
"""
from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import audit, orders as order_logic, runtime
from ..config import get_settings
from ..dhan_client import DhanAPIError, get_dhan_client
from ..security import require_csrf, require_session, verify_order_pin

router = APIRouter(prefix="/api/orders", tags=["orders"])


class OrderPreviewBody(BaseModel):
    indexKey: str
    securityId: str
    optionType: str
    transactionType: str
    side: str = ""
    quantity: int = Field(gt=0, le=100000)
    price: float = Field(gt=0)
    targetPct: float = Field(gt=0, le=100)
    productType: str = "INTRADAY"
    orderType: str = "LIMIT"
    noStopLoss: bool = True
    stopLossPct: float | None = None
    # When True (default) `quantity` is LOTS; the server multiplies by the
    # index's lot size to get exchange units. Set False to send raw units.
    lots: bool = True


class OrderPlaceBody(OrderPreviewBody):
    pin: str


def _to_order_request(body: OrderPreviewBody) -> order_logic.OrderRequest:
    return order_logic.OrderRequest(
        index_key=body.indexKey.upper(),
        security_id=str(body.securityId),
        option_type=body.optionType.upper(),
        transaction_type=body.transactionType.upper(),
        side=body.side,
        quantity=body.quantity,
        price=float(body.price),
        target_pct=float(body.targetPct),
        product_type=body.productType,
        order_type=body.orderType,
        no_stop_loss=body.noStopLoss,
        stop_loss_pct=body.stopLossPct,
        lots=body.lots,
    )


@router.post("/preview")
async def preview(
    body: OrderPreviewBody,
    _session: str = Depends(require_session),
    _csrf: None = Depends(require_csrf),
):
    """Compute target + safety stop and return exactly what will be sent."""
    req = _to_order_request(body)
    payload = order_logic.build_super_order(req)
    return {
        "ok": True,
        "preview": order_logic.preview(req),
        "dhanPayload": payload,  # shown in the confirm popup for transparency
    }


@router.post("/place")
async def place(
    body: OrderPlaceBody,
    _session: str = Depends(require_session),
    _csrf: None = Depends(require_csrf),
):
    verify_order_pin(body.pin)
    s = get_settings()
    req = _to_order_request(body)
    payload = order_logic.build_super_order(req)
    payload["dhanClientId"] = s.dhan_client_id

    pv = order_logic.preview(req)

    if runtime.is_dry_run():
        audit.log_order(
            action="place_super_order",
            success=True,
            dry_run=True,
            status="DRY_RUN",
            message="Dry-run: not sent to Dhan",
            payload=payload,
            index_key=req.index_key,
            option_type=req.option_type,
            transaction_type=req.transaction_type,
            quantity=req.quantity,
            price=payload["price"],
            target_price=payload["targetPrice"],
            stop_loss_price=payload["stopLossPrice"],
        )
        return {"ok": True, "dryRun": True, "status": "DRY_RUN", "preview": pv, "payload": payload}

    try:
        result = await get_dhan_client().place_super_order(payload)
    except DhanAPIError as exc:
        audit.log_order(
            action="place_super_order", success=False, dry_run=False,
            status=exc.error_code, message=exc.error_message, payload=payload,
            index_key=req.index_key, option_type=req.option_type,
            transaction_type=req.transaction_type, quantity=req.quantity,
            price=payload["price"], target_price=payload["targetPrice"],
            stop_loss_price=payload["stopLossPrice"],
        )
        raise HTTPException(status_code=502, detail=exc.to_dict())

    audit.log_order(
        action="place_super_order", success=True, dry_run=False,
        order_id=str(result.get("orderId")), status=str(result.get("orderStatus")),
        message="Super order placed", payload=payload,
        index_key=req.index_key, option_type=req.option_type,
        transaction_type=req.transaction_type, quantity=req.quantity,
        price=payload["price"], target_price=payload["targetPrice"],
        stop_loss_price=payload["stopLossPrice"],
    )
    return {"ok": True, "dryRun": False, "result": result, "preview": pv}


@router.post("/cancel")
async def cancel(
    body: Dict[str, str],
    _session: str = Depends(require_session),
    _csrf: None = Depends(require_csrf),
):
    order_id = body.get("orderId")
    leg = body.get("leg") or "ENTRY_LEG"
    pin = body.get("pin")
    if not order_id:
        raise HTTPException(status_code=400, detail="orderId required")
    verify_order_pin(pin)
    if runtime.is_dry_run():
        audit.log_order(action="cancel_super_order", success=True, dry_run=True,
                        order_id=order_id, status="DRY_RUN", message=f"Dry-run cancel {leg}")
        return {"ok": True, "dryRun": True, "status": "DRY_RUN"}
    try:
        result = await get_dhan_client().cancel_super_order(order_id, leg)
    except DhanAPIError as exc:
        audit.log_order(action="cancel_super_order", success=False, dry_run=False,
                        order_id=order_id, status=exc.error_code, message=exc.error_message)
        raise HTTPException(status_code=502, detail=exc.to_dict())
    audit.log_order(action="cancel_super_order", success=True, dry_run=False,
                    order_id=order_id, status=str(result.get("orderStatus")),
                    message=f"Cancelled {leg}")
    return {"ok": True, "dryRun": False, "result": result}


@router.get("/super")
async def super_orders(_session: str = Depends(require_session)):
    try:
        return await get_dhan_client().get_super_orders()
    except DhanAPIError as exc:
        raise HTTPException(status_code=502, detail=exc.to_dict())


@router.get("/book")
async def order_book(_session: str = Depends(require_session)):
    try:
        return await get_dhan_client().get_order_book()
    except DhanAPIError as exc:
        raise HTTPException(status_code=502, detail=exc.to_dict())


@router.get("/trades")
async def trades(_session: str = Depends(require_session)):
    try:
        return await get_dhan_client().get_trades()
    except DhanAPIError as exc:
        raise HTTPException(status_code=502, detail=exc.to_dict())
