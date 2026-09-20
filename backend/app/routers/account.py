"""Account endpoints — funds, positions, holdings, IP status + IP registration."""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import audit, marketdata, runtime
from ..config import get_settings
from ..dhan_client import DhanAPIError, get_dhan_client
from ..runtime import is_dry_run, is_fast_orders
from ..security import require_csrf, require_session, verify_order_pin

router = APIRouter(prefix="/api/account", tags=["account"])


def _wrap(coro_factory):
    async def _inner():
        try:
            return await coro_factory()
        except DhanAPIError as exc:
            raise HTTPException(status_code=502, detail=exc.to_dict())
    return _inner


@router.get("/funds")
async def funds(_session: str = Depends(require_session)):
    try:
        return await get_dhan_client().get_fund_limit()
    except DhanAPIError as exc:
        raise HTTPException(status_code=502, detail=exc.to_dict())


@router.get("/positions")
async def positions(_session: str = Depends(require_session)):
    try:
        return await get_dhan_client().get_positions()
    except DhanAPIError as exc:
        raise HTTPException(status_code=502, detail=exc.to_dict())


@router.get("/holdings")
async def holdings(_session: str = Depends(require_session)):
    try:
        return await get_dhan_client().get_holdings()
    except DhanAPIError as exc:
        raise HTTPException(status_code=502, detail=exc.to_dict())


@router.get("/ip")
async def ip_status(_session: str = Depends(require_session)):
    client = get_dhan_client()
    registered: Any = None
    registered_err = None
    try:
        registered = await client.get_ip()
    except DhanAPIError as exc:
        registered_err = exc.to_dict()
    detected = await client.detect_public_ip()
    return {
        "registered": registered,
        "registeredError": registered_err,
        "detectedIp": detected,
        "match": _ip_match(registered, detected),
    }


def _ip_match(registered: Any, detected: Any) -> bool | None:
    """True if `detected` IP appears in the registered IP payload."""
    if not registered or not detected:
        return None
    blob = str(registered)
    return str(detected) in blob


class SetIpBody(BaseModel):
    ip: str | None = None  # if omitted, we auto-detect and register that
    flag: str = "PRIMARY"


@router.post("/ip/register")
async def register_ip(
    body: SetIpBody,
    _session: str = Depends(require_session),
    _csrf: None = Depends(require_csrf),
):
    """Register (whitelist) the outbound IP with Dhan so live orders work.

    This is the fix for the 'Invalid IP' / DH-905 error on order placement.
    """
    client = get_dhan_client()
    ip = body.ip
    if not ip:
        ip = await client.detect_public_ip()
    if not ip:
        raise HTTPException(
            status_code=400,
            detail="Could not auto-detect your public IP. Enter it manually.",
        )
    flag = (body.flag or "PRIMARY").upper()
    if flag not in ("PRIMARY", "SECONDARY"):
        raise HTTPException(status_code=400, detail="flag must be PRIMARY or SECONDARY")
    try:
        result = await client.set_ip(ip, flag)
    except DhanAPIError as exc:
        audit.log_order(
            action="set_ip", success=False, dry_run=False,
            status=exc.error_code, message=exc.error_message,
            payload={"ip": ip, "flag": flag},
        )
        raise HTTPException(status_code=502, detail=exc.to_dict())
    audit.log_order(
        action="set_ip", success=True, dry_run=False,
        status=str(result.get("status", "SUCCESS")) if isinstance(result, dict) else "SUCCESS",
        message=f"Registered {flag} IP {ip}",
        payload={"ip": ip, "flag": flag, "result": result},
    )
    return {"ok": True, "ip": ip, "flag": flag, "result": result}


class SquareOffBody(BaseModel):
    securityId: str
    exchangeSegment: str = "NSE_FNO"
    quantity: int
    productType: str = "INTRADAY"
    transactionType: str  # SELL to exit a long, BUY to exit a short


@router.post("/squareoff")
async def square_off(
    body: SquareOffBody,
    _session: str = Depends(require_session),
    _csrf: None = Depends(require_csrf),
):
    """One-click EXIT — square off a position with a MARKET order.

    Uses the standard /orders endpoint (a plain market order), NOT a super
    order — an exit must fill immediately and carry no target/SL legs.
    """
    if body.quantity <= 0:
        raise HTTPException(status_code=400, detail="quantity must be > 0")
    txn = body.transactionType.upper()
    if txn not in ("BUY", "SELL"):
        raise HTTPException(status_code=400, detail="transactionType must be BUY or SELL")

    s = get_settings()
    payload: Dict[str, Any] = {
        "dhanClientId": s.dhan_client_id,
        "transactionType": txn,
        "exchangeSegment": body.exchangeSegment,
        "productType": body.productType,
        "orderType": "MARKET",
        "validity": "DAY",
        "securityId": str(body.securityId),
        "quantity": int(body.quantity),
        "price": 0,
        "triggerPrice": 0,
        "afterMarketOrder": False,
    }

    if runtime.is_dry_run():
        audit.log_order(
            action="squareoff", success=True, dry_run=True, status="DRY_RUN",
            message=f"Dry-run square-off {txn} qty={body.quantity}", payload=payload,
        )
        return {"ok": True, "dryRun": True, "status": "DRY_RUN"}

    try:
        result = await get_dhan_client().place_order(payload)
    except DhanAPIError as exc:
        audit.log_order(
            action="squareoff", success=False, dry_run=False,
            status=exc.error_code, message=exc.error_message, payload=payload,
        )
        raise HTTPException(status_code=502, detail=exc.to_dict())

    audit.log_order(
        action="squareoff", success=True, dry_run=False,
        order_id=str(result.get("orderId")),
        status=str(result.get("orderStatus")), message="Square-off placed", payload=payload,
    )
    return {"ok": True, "dryRun": False, "result": result}


@router.get("/cache-state")
async def cache_state(_session: str = Depends(require_session)):
    """Show whether the daily-OHLC history cache is warmed (from login)."""
    state = marketdata.cache_state()
    warmed = bool(state) and all(v["candles"] > 0 for v in state.values())
    return {"ok": True, "warmed": warmed, "indices": state}


@router.post("/warm-cache")
async def warm_cache(
    _session: str = Depends(require_session),
    _csrf: None = Depends(require_csrf),
):
    """Manually (re)warm the history cache — useful after a token/IP change."""
    result = await marketdata.warm_up()
    return {"ok": True, "indices": result}


@router.get("/status")
async def status(_session: str = Depends(require_session)):
    """A quick 'is order path healthy?' summary for the Connection panel."""
    s = get_settings()
    try:
        profile = await get_dhan_client().get_profile()
        profile_ok, profile_err = True, None
    except DhanAPIError as exc:
        profile_ok, profile_err, profile = False, exc.to_dict(), {}
    try:
        ip_info = await get_dhan_client().get_ip()
        ip_err = None
    except DhanAPIError as exc:
        ip_info, ip_err = {}, exc.to_dict()
    return {
        "dryRun": is_dry_run(),
        "fastOrders": is_fast_orders(),
        "profileOk": profile_ok,
        "profileError": profile_err,
        "activeSegment": profile.get("activeSegment"),
        "dataPlan": profile.get("dataPlan"),
        "ip": ip_info,
        "ipError": ip_err,
    }
