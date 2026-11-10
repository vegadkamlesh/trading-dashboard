"""Order endpoints — preview, place (super order), cancel, and book views.

Safety:
  * session required,
  * order PIN required for anything that can move money,
  * DRY-RUN mode (default) validates + audits but does NOT hit Dhan.
"""
from __future__ import annotations

from typing import Any, Dict
import hashlib
import json
import math
import sqlite3
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import audit, orders as order_logic, runtime
from ..algo import algo
from ..config import get_settings
from ..dhan_client import DhanAPIError, get_trading_client as get_dhan_client
from ..accounts import current_account, selected_accounts
from ..optionchain import engine as chain_engine
from ..feed import attach_ltp
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
    requestId: UUID = Field(default_factory=uuid4)


async def _fund_check(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Compare the order's required cash against the available balance.

    Unknown balance is an explicit failure, never approval to place an order.
    """
    required = order_logic.required_cash(payload)
    available: float | None = None
    try:
        f = await get_dhan_client().get_fund_limit()
        if isinstance(f, dict):
            # Start-of-day funds are not the currently available balance.
            raw = f.get("availabelBalance")
            if raw is not None:
                available = float(raw)
    except DhanAPIError as exc:
        return {"requiredCash": required, "availableBalance": None, "sufficient": False,
                "shortfall": 0.0, "error": exc.error_message}
    except (ValueError, TypeError):
        return {"requiredCash": required, "availableBalance": None, "sufficient": False,
                "shortfall": 0.0, "error": "Broker returned an invalid available balance."}
    if available is None or not math.isfinite(available) or available < 0:
        return {"requiredCash": required, "availableBalance": None, "sufficient": False,
                "shortfall": 0.0, "error": "Broker did not provide a valid available balance."}
    sufficient = available >= required
    return {
        "requiredCash": required,
        "availableBalance": available,
        "sufficient": sufficient,
        "shortfall": 0.0 if sufficient or available is None else round(required - available, 2),
    }


def _to_order_request(body: OrderPreviewBody) -> order_logic.OrderRequest:
    req = order_logic.OrderRequest(
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
    if req.order_type == "MARKET":
        snap = chain_engine.snapshot(req.index_key)
        if not snap or snap.get("stale") or snap.get("error") or snap.get("ageSeconds", 999) > 12:
            raise HTTPException(409, "Fresh option quote unavailable. Wait for market sync before a MARKET order.")
        node = next((row.get("ce" if req.option_type == "CE" else "pe") or {}
                     for row in snap.get("rows", [])
                     if str((row.get("ce" if req.option_type == "CE" else "pe") or {}).get("securityId")) == req.security_id), None)
        premium = float(node.get("ltp") or 0) if node else 0
        if not math.isfinite(premium) or premium <= 0:
            raise HTTPException(409, "Fresh price for this exact option contract is unavailable.")
        req.price = order_logic._round_tick(premium)
    return req


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
        "funds": await _fund_check(payload),
    }


@router.post("/place")
async def place(
    body: OrderPlaceBody,
    _session: str = Depends(require_session),
    _csrf: None = Depends(require_csrf),
):
    if current_account() not in selected_accounts():
        raise HTTPException(409, "This account is disabled for new entries")
    async with algo._execution_lock:
        return await _place_locked(body)


async def _place_locked(body: OrderPlaceBody):
    verify_order_pin(body.pin)
    request_id = str(body.requestId)
    fingerprint = hashlib.sha256(json.dumps(
        body.model_dump(exclude={"pin", "requestId"}), sort_keys=True,
    ).encode()).hexdigest()
    if not runtime.is_dry_run():
        try:
            exists, previous = audit.read_manual_request(request_id, fingerprint)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        except sqlite3.Error as exc:
            raise HTTPException(503, "Cannot recover order identity. Order not sent.") from exc
        if exists:
            if previous:
                return previous
            raise HTTPException(409, "Previous submission has an unknown/pending outcome. Check Super Orders at Dhan; do not repeat the BUY/SELL.")
    s = get_settings()
    req = _to_order_request(body)
    payload = order_logic.build_super_order(req)
    payload["dhanClientId"] = get_dhan_client()._client_id
    tracked = algo._position
    if (tracked and str(tracked.security_id) == str(payload["securityId"])
            and algo._segment_for(tracked.index_key) == payload["exchangeSegment"]):
        raise HTTPException(409, "This contract is managed by the algo. Use its EXIT; do not mix manual entry quantity.")
    from ..algo2.engine import group as algo2_group
    tracked2 = algo2_group.items[current_account()]._position
    if (tracked2 and str(tracked2.security_id) == str(payload["securityId"])
            and algo2_group.master._segment_for(tracked2.index_key) == payload["exchangeSegment"]):
        raise HTTPException(409, "This contract is managed by Algo 2. Use its EXIT; do not mix manual entry quantity.")

    pv = order_logic.preview(req)

    # Balance check: block an order that costs more than the available balance
    # so the trader can reduce lots instead of getting a broker rejection.
    funds = await _fund_check(payload)
    if funds.get("error"):
        raise HTTPException(503, f"Cannot confirm funds: {funds['error']}. Order not sent.")
    if not funds["sufficient"]:
        msg = (
            f"Insufficient balance: order needs ₹{funds['requiredCash']:.2f} "
            f"but only ₹{(funds['availableBalance'] or 0):.2f} available "
            f"(short by ₹{funds['shortfall']:.2f}). Reduce the lots and try again."
        )
        raise HTTPException(status_code=400, detail=msg)

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

    payload["correlationId"] = f"MAN-E-{body.requestId.hex[:24]}"
    try:
        claimed, previous = audit.claim_manual_request(request_id, fingerprint)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except sqlite3.Error as exc:
        raise HTTPException(503, "Cannot persist order identity. Order not sent.") from exc
    if not claimed:
        if previous:
            return previous
        raise HTTPException(409, "Previous submission has an unknown/pending outcome. Check Super Orders at Dhan; do not repeat the BUY/SELL.")
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

    status = str(result.get("orderStatus") or "").upper()
    accepted = bool(result.get("orderId")) and status in {"TRANSIT", "PENDING", "PART_TRADED", "TRADED"}
    error = None if accepted else (
        result.get("omsErrorDescription") or result.get("errorMessage")
        or f"Super Order status {status or 'unknown'}; verify this account at Dhan before retrying."
    )
    audit.log_order(
        action="place_super_order", success=accepted, dry_run=False,
        order_id=str(result.get("orderId")), status=str(result.get("orderStatus")),
        message="Super order submitted (fill not guaranteed)" if accepted else str(error), payload=payload,
        index_key=req.index_key, option_type=req.option_type,
        transaction_type=req.transaction_type, quantity=req.quantity,
        price=payload["price"], target_price=payload["targetPrice"],
        stop_loss_price=payload["stopLossPrice"],
    )
    response = {"ok": accepted, "dryRun": False, "result": result, "preview": pv, "error": error}
    try:
        audit.finish_manual_request(request_id, response)
    except sqlite3.Error as exc:
        raise HTTPException(503, "Order submitted but receipt could not be saved. Check Dhan; do not place it again.") from exc
    return response


@router.post("/cancel")
async def cancel(
    body: Dict[str, str],
    _session: str = Depends(require_session),
    _csrf: None = Depends(require_csrf),
):
    async with algo._execution_lock:
        return await _cancel_locked(body)


async def _cancel_locked(body: Dict[str, str]):
    order_id = body.get("orderId")
    leg = body.get("leg") or "ENTRY_LEG"
    pin = body.get("pin")
    if not order_id:
        raise HTTPException(status_code=400, detail="orderId required")
    if leg not in {"ENTRY_LEG", "TARGET_LEG", "STOP_LOSS_LEG"}:
        raise HTTPException(400, "Invalid Super Order leg")
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
                    message=f"Cancellation requested for {leg}; confirmation pending")
    return {"ok": True, "dryRun": False, "result": result}


@router.get("/super")
async def super_orders(_session: str = Depends(require_session)):
    try:
        rows = await get_dhan_client().get_super_orders()
        if not isinstance(rows, list):
            raise DhanAPIError(0, "BAD_ORDERS", "Invalid broker Super Order response")
        return await attach_ltp(rows)
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
