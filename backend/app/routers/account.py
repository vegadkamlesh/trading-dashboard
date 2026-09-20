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

    # Dhan tells us directly whether orders are allowed from this IP, and what
    # IP IT sees us as (detectedIP). Prefer Dhan's own verdict over our guess.
    orders_allowed = None
    ip_match_status = None
    dhan_seen_ip = None
    if isinstance(registered, dict):
        orders_allowed = registered.get("ordersAllowed")
        ip_match_status = registered.get("ipMatchStatus")
        dhan_seen_ip = registered.get("detectedIP")

    return {
        "registered": registered,
        "registeredError": registered_err,
        "detectedIp": detected,
        "dhanSeenIp": dhan_seen_ip,
        "ipMatchStatus": ip_match_status,
        "ordersAllowed": orders_allowed,
        # Dhan uses variants like "PRIMARY_MATCH" / "SECONDARY_MATCH".
        "match": ("MATCH" in str(ip_match_status).upper())
        if ip_match_status
        else _ip_match(registered, detected),
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

    # Dhan returns HTTP 200 even when the IP is rejected — read the real status.
    dhan_status = str(result.get("status", "")).upper() if isinstance(result, dict) else ""
    message = str(result.get("message", "")) if isinstance(result, dict) else ""
    # "IP already added." is a benign success (the IP IS registered).
    already = "already" in message.lower()
    ok = dhan_status in ("SUCCESS", "") or already

    audit.log_order(
        action="set_ip", success=ok, dry_run=False,
        status=dhan_status or "SUCCESS",
        message=f"{flag} IP {ip}: {message or dhan_status}",
        payload={"ip": ip, "flag": flag, "result": result},
    )

    # Re-read the live verdict so the UI shows whether orders are now allowed.
    verdict: Dict[str, Any] = {}
    try:
        verdict = await client.get_ip() or {}
    except DhanAPIError:
        verdict = {}

    return {
        "ok": ok,
        "ip": ip,
        "flag": flag,
        "status": dhan_status,
        "message": message,
        "alreadyAdded": already,
        "ordersAllowed": verdict.get("ordersAllowed"),
        "ipMatchStatus": verdict.get("ipMatchStatus"),
        "dhanSeenIp": verdict.get("detectedIP"),
        "result": result,
    }


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
    await marketdata.prime_history_studies()
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
