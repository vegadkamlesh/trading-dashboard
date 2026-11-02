"""Order construction & validation.

Strategy — Dhan Super Order:
---------------------------
The user wants: "buy at a LIMIT price, and set a profit target (e.g. 2% / 5%),
no stop loss."

Dhan's Super Order *requires* a stop-loss leg (exchange rule). So we place:
  * ENTRY_LEG : LIMIT buy at the user's price
  * TARGET_LEG: SELL at entry * (1 + target%)
  * STOP_LOSS_LEG: SELL at entry * (1 - safety_sl%), where safety_sl% is the
    exchange-minimum distance, computed to be as far as reasonable.

The confirm popup in the UI shows the computed target & the (far) safety SL so
the user always knows exactly what will be sent.

All values are validated server-side. No raw passthrough to Dhan.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Optional

from fastapi import HTTPException

from .instruments import get_index, lot_size_for

# Conservative bounds. Tighten as you like.
MIN_TARGET_PCT = 0.1
MAX_TARGET_PCT = 100.0
MIN_SAFETY_SL_PCT = 3.0   # distance of the mandatory safety stop (far away)
MAX_SAFETY_SL_PCT = 90.0


@dataclass
class OrderRequest:
    index_key: str
    security_id: str
    option_type: str          # CE | PE
    transaction_type: str     # BUY | SELL
    side: str                 # informational
    quantity: int             # raw value as entered (LOTS, unless lots=False)
    price: float
    target_pct: float
    product_type: str = "INTRADAY"
    validity: str = "DAY"
    order_type: str = "LIMIT"
    correlation_id: Optional[str] = None
    # When True, the mandatory Super-Order SL leg is pushed WAY out (near-zero)
    # Default True means a far stop, not guaranteed protection or no stop.
    no_stop_loss: bool = True
    # Optional user-specified stop distance (%). Ignored when no_stop_loss=True.
    stop_loss_pct: Optional[float] = None
    # When True (default), `quantity` is interpreted as LOTS and multiplied by
    # the index's lot size to get the exchange UNITS sent to Dhan. Set False to
    # pass raw units through (e.g. square-off which already sends units).
    lots: bool = True


def units_for(req: OrderRequest) -> int:
    """Exchange units = lots × lot size (or the raw quantity if lots=False)."""
    if not req.lots:
        return int(req.quantity)
    lot_size = lot_size_for(req.index_key)
    return int(req.quantity) * int(lot_size)


def required_cash(payload: Dict[str, Any]) -> float:
    """Approx cash the order needs, from the built Dhan payload.

    For BUY options the outlay is simply the premium = price × units, which is
    exactly what the exchange debits. (For SELL the exchange blocks SPAN+exposure
    margin we can't compute exactly, so this figure is only a *lower-bound hint*;
    we still surface it so the trader gets a heads-up.)
    """
    try:
        price = float(payload.get("price") or 0)
        units = int(payload.get("quantity") or 0)
    except (TypeError, ValueError):
        return 0.0
    return round(price * units, 2)


def _round_tick(value: float, tick: float = 0.05) -> float:
    """Round to nearest exchange tick (NSE options tick = 0.05)."""
    if tick <= 0:
        return round(value, 2)
    return round(round(value / tick) * tick, 2)


def build_super_order(req: OrderRequest) -> Dict[str, Any]:
    inst = get_index(req.index_key)
    if inst is None:
        raise HTTPException(status_code=400, detail=f"Unknown index '{req.index_key}'")

    if req.option_type not in ("CE", "PE"):
        raise HTTPException(status_code=400, detail="option_type must be CE or PE")
    if req.transaction_type not in ("BUY", "SELL"):
        raise HTTPException(status_code=400, detail="transaction_type must be BUY or SELL")
    if req.quantity <= 0:
        raise HTTPException(status_code=400, detail="quantity must be > 0")
    if not math.isfinite(req.price) or req.price <= 0:
        raise HTTPException(status_code=400, detail="price must be > 0")
    if not (MIN_TARGET_PCT <= req.target_pct <= MAX_TARGET_PCT):
        raise HTTPException(
            status_code=400,
            detail=f"target_pct must be between {MIN_TARGET_PCT} and {MAX_TARGET_PCT}",
        )
    if req.product_type not in ("INTRADAY", "MARGIN", "CNC", "MTF"):
        raise HTTPException(status_code=400, detail="invalid product_type")
    if req.order_type not in ("LIMIT", "MARKET"):
        raise HTTPException(status_code=400, detail="invalid order_type")
    if not str(req.security_id).isdigit() or int(req.security_id) <= 0:
        raise HTTPException(status_code=400, detail="A valid option security ID is required")
    units = units_for(req)
    if units <= 0 or units % lot_size_for(req.index_key):
        raise HTTPException(status_code=400, detail="Quantity must be a whole exchange lot")
    entry = _round_tick(req.price)
    if entry <= 0 or abs(entry - req.price) > .000001:
        raise HTTPException(status_code=400, detail="Reference/limit price must be on a 0.05 tick")
    if req.stop_loss_pct is not None and not math.isfinite(req.stop_loss_pct):
        raise HTTPException(status_code=400, detail="Stop loss percentage must be finite")

    # Decide the stop distance:
    #  * no_stop_loss=True  -> push SL far out (effectively "no SL")
    #  * stop_loss_pct set  -> user's chosen distance (bounded)
    #  * otherwise          -> the exchange-minimum safety distance
    if req.no_stop_loss:
        sl_pct = MAX_SAFETY_SL_PCT
    elif req.stop_loss_pct is not None:
        sl_pct = max(1.0, min(float(req.stop_loss_pct), 95.0))
    else:
        sl_pct = MIN_SAFETY_SL_PCT

    # Target & safety stop computed from entry price, direction-aware.
    if req.transaction_type == "BUY":
        target_price = _round_tick(req.price * (1 + req.target_pct / 100.0))
        stop_loss_price = _round_tick(req.price * (1 - sl_pct / 100.0))
    else:  # SELL (short) — target is below, safety stop above
        target_price = _round_tick(req.price * (1 - req.target_pct / 100.0))
        stop_loss_price = _round_tick(req.price * (1 + sl_pct / 100.0))

    # The exchange requires a positive SL price; clamp to the smallest tick.
    if stop_loss_price <= 0:
        stop_loss_price = 0.05
    if not (0 < stop_loss_price < entry < target_price if req.transaction_type == "BUY"
            else 0 < target_price < entry < stop_loss_price):
        raise HTTPException(400, "Rounded target and SL must be positive and on opposite sides of entry; increase the target/distance.")

    return {
        "transactionType": req.transaction_type,
        "exchangeSegment": "NSE_FNO" if req.index_key in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY") else "BSE_FNO",
        "productType": req.product_type,
        "orderType": req.order_type,
        "securityId": str(req.security_id),
        "quantity": units,
        "price": entry,
        "targetPrice": float(target_price),
        "stopLossPrice": float(stop_loss_price),
        "trailingJump": 0,
    }


def preview(req: OrderRequest) -> Dict[str, Any]:
    """What the confirm popup should display. No network call."""
    payload = build_super_order(req)
    lot_size = lot_size_for(req.index_key)
    return {
        "index": req.index_key,
        "optionType": req.option_type,
        "side": req.side,
        "quantity": req.quantity,
        "lots": req.quantity,
        "lotSize": lot_size,
        "units": payload["quantity"],
        "entryPrice": _round_tick(req.price),
        "targetPrice": payload["targetPrice"],
        "expectedProfitPerUnit": round(payload["targetPrice"] - req.price, 2)
        if req.transaction_type == "BUY"
        else round(req.price - payload["targetPrice"], 2),
        "safetyStopPrice": payload["stopLossPrice"],
        "safetyStopNote": (
            "Far stop: 90% from the entry reference premium. It CAN trigger and "
            "losses can be large. Target or manual EXIT normally closes earlier."
            if req.no_stop_loss
            else "Stop-loss leg at your chosen distance."
        ),
        "noStopLoss": req.no_stop_loss,
        "exchangeSegment": payload["exchangeSegment"],
        "productType": payload["productType"],
    }
