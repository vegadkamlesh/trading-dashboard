"""Account endpoints — funds, positions, holdings, IP status + IP registration."""
from __future__ import annotations

from typing import Any, Dict
import uuid
import sqlite3
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import audit, marketdata, runtime
from ..algo import algo, TERMINAL_ORDERS
from ..config import get_settings
from ..dhan_client import DhanAPIError, get_trading_client as get_dhan_client
from ..accounts import current_account
from ..feed import attach_ltp
from ..runtime import is_dry_run, is_fast_orders
from ..security import require_csrf, require_session, verify_order_pin

router = APIRouter(prefix="/api/account", tags=["account"])
_pending_exits: Dict[tuple[str, str, str], str] = {}
_child_pending_exits: Dict[tuple[str, str, str], str] = {}
logger = logging.getLogger("dhan.account")


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


@router.get("/sync")
async def sync_status(_session: str = Depends(require_session)):
    return get_dhan_client().sync_status()


@router.get("/positions")
async def positions(_session: str = Depends(require_session)):
    pending = _pending_exits if current_account() == "master" else _child_pending_exits
    try:
        rows = await get_dhan_client().get_positions()
        if not isinstance(rows, list):
            raise DhanAPIError(0, "BAD_POSITIONS", "Invalid broker positions response")
        pending.update(audit.pending_manual_exits())
        for key in list(pending):
            sid, segment, product = key
            held = sum(int(r.get("netQty") or 0) for r in rows
                       if str(r.get("securityId")) == sid and r.get("exchangeSegment") == segment
                       and r.get("productType") == product)
            if held == 0:
                try:
                    order = await get_dhan_client().get_order_by_correlation(pending[key])
                    if not isinstance(order, dict) or not order.get("orderStatus"):
                        raise DhanAPIError(0, "UNKNOWN_EXIT", "Pending manual exit status unavailable.")
                    if str(order.get("orderStatus") or "").upper() in TERMINAL_ORDERS:
                        audit.save_manual_exit(key, None)
                        pending.pop(key, None)
                except DhanAPIError as exc:
                    logger.warning("account=%s manual_exit_unverified code=%s reason=%s; broker holding flat, identity retained",
                                   current_account(), exc.error_code, exc.error_message)
        return await attach_ltp(rows)
    except DhanAPIError as exc:
        raise HTTPException(status_code=502, detail=exc.to_dict())
    except sqlite3.Error as exc:
        raise HTTPException(503, "Cannot recover manual exit state; check Dhan before another EXIT.") from exc


@router.get("/holdings")
async def holdings(_session: str = Depends(require_session)):
    try:
        return await get_dhan_client().get_holdings()
    except DhanAPIError as exc:
        raise HTTPException(status_code=502, detail=exc.to_dict())


def _ip_match_status(registered: Any) -> bool | None:
    """Dhan's own verdict on whether our outbound IP is whitelisted.

    Dhan returns `ordersAllowed` (bool) plus `ipMatchStatus` such as
    "PRIMARY_MATCH"/"SECONDARY_MATCH"/"MISMATCH".

    Do NOT substring-match "MATCH": "MISMATCH" contains it, which made every
    mismatch report as a match - so the UI blamed Dhan for a stale local IP.
    """
    if not isinstance(registered, dict):
        return None
    allowed = registered.get("ordersAllowed")
    if allowed is not None:
        return bool(allowed)
    status = str(registered.get("ipMatchStatus") or "").upper()
    if not status:
        return None
    return status in ("PRIMARY_MATCH", "SECONDARY_MATCH")


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
        "match": _ip_match_status(registered)
        if _ip_match_status(registered) is not None
        else _ip_match(registered, detected),
    }


def _ip_match(registered: Any, detected: Any) -> bool | None:
    """True if `detected` IP appears in the registered IP payload."""
    if not registered or not detected:
        return None
    blob = str(registered)
    return str(detected) in blob


@router.get("/ip/diagnose")
async def ip_diagnose(_session: str = Depends(require_session)):
    """End-to-end IP diagnostic for the order path.

    Dhan's /ip/getIP endpoint can report `PRIMARY_MATCH` + `ordersAllowed: true`
    while the ORDER engine still rejects with DH-905 "Invalid IP" (a known
    Dhan-side inconsistency where the two subsystems hold different IP records).

    This probe compares BOTH: the getIP verdict AND a REAL (harmless) order
    probe — an invalid securityId that Dhan rejects for a *different* reason
    when the IP is accepted. If the probe comes back "Invalid IP", the problem
    is on Dhan's side (their order engine's IP record is stale/corrupt), not
    ours, and the fix is to remove & re-add the static IP from Dhan's website.
    """
    client = get_dhan_client()

    # 1) What Dhan's IP endpoint says.
    verdict: Dict[str, Any] = {}
    err = None
    try:
        verdict = await client.get_ip() or {}
    except DhanAPIError as exc:
        err = exc.to_dict()

    getip_ok = bool(_ip_match_status(verdict))

    # 2) What the ORDER engine actually does — a deliberately bad order. If the
    #    IP is accepted, Dhan complains about the (bogus) securityId instead of
    #    the IP. If it still says "Invalid IP", the IP is being rejected.
    probe_ok: bool | None = None
    probe_code: str | None = None
    probe_message: str | None = None
    try:
        await client.place_order(
            {
                "dhanClientId": client._client_id,
                "transactionType": "BUY",
                "exchangeSegment": "NSE_FNO",
                "productType": "INTRADAY",
                "orderType": "LIMIT",
                "validity": "DAY",
                "securityId": "0",  # bogus on purpose → never executes
                "quantity": 1,
                "price": 1,
                "triggerPrice": 0,
                "afterMarketOrder": False,
            }
        )
        # If it somehow succeeds, the IP is definitely fine.
        probe_ok = True
    except DhanAPIError as exc:
        probe_code = exc.error_code
        probe_message = exc.error_message
        # If the complaint is NOT about the IP, then the IP is accepted.
        probe_ok = "invalid ip" not in (exc.error_message or "").lower()

    return {
        "getIpReports": {
            "ordersAllowed": verdict.get("ordersAllowed"),
            "ipMatchStatus": verdict.get("ipMatchStatus"),
            "detectedIP": verdict.get("detectedIP"),
            "primaryIP": verdict.get("primaryIP"),
            "secondaryIP": verdict.get("secondaryIP"),
            "modifyDatePrimary": verdict.get("modifyDatePrimary"),
            "error": err,
        },
        "orderEngineProbe": {
            "ipAccepted": probe_ok,
            "errorCode": probe_code,
            "errorMessage": probe_message,
        },
        # The real verdict: does the ORDER engine accept our IP?
        "orderPathHealthy": probe_ok,
        # A human-readable diagnosis for the UI.
        "diagnosis": _diagnose(getip_ok, probe_ok, verdict),
    }


def _diagnose(getip_ok: bool, probe_ok: bool | None, verdict: Dict[str, Any]) -> Dict[str, Any]:
    if probe_ok:
        return {
            "level": "ok",
            "title": "IP sahi hai — orders chalenge",
            "detail": "Dhan ke order engine ne aapka IP accept kar liya. Sab theek hai.",
            "action": "",
        }
    if getip_ok and probe_ok is False:
        mod = verdict.get("modifyDatePrimary")
        return {
            "level": "dhan-side",
            "title": "Dhan ke side pe IP record kharab hai (getIP match, par order reject)",
            "detail": (
                "Dhan ka /ip/getIP endpoint keh raha hai IP theek hai "
                f"(PRIMARY_MATCH, ordersAllowed=true, modifyDate={mod}), LEKIN order engine "
                "DH-905 'Invalid IP' de raha hai. Matlab Dhan ke andar do subsystem alag IP record "
                "rakhte hain — ye Dhan ka bug hai, aapke system ka nahi."
            ),
            "action": (
                "Fix: Dhan website (web.dhan.co) → My Profile → Static IP → apna IP DELETE karo, "
                "phir dobara ADD/Verify karo. 5-10 min baad order try karo. Agar phir bhi na chale to "
                "Dhan support (help@dhan.co) ko bolo: 'DH-905 Invalid IP aata hai par /ip/getIP "
                "PRIMARY_MATCH batata hai — mera static IP record reset karo'."
            ),
        }
    return {
        "level": "client-side",
        "title": "Aapka IP Dhan par whitelisted nahi hai",
        "detail": "Dhan ne aapka outbound IP accept nahi kiya. Neeche 'Register my IP' dabao.",
        "action": "Settings ▸ Register my IP dabao, phir diagnose dobara chalao.",
    }


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
    async with algo._execution_lock:
        return await _square_off_locked(body)


async def _square_off_locked(body: SquareOffBody):
    """One-click EXIT — square off a position with a MARKET order.

    Uses the standard /orders endpoint (a plain market order), NOT a super
    order — an exit must fill immediately and carry no target/SL legs.
    """
    pending = _pending_exits if current_account() == "master" else _child_pending_exits
    if body.quantity <= 0:
        raise HTTPException(status_code=400, detail="quantity must be > 0")
    txn = body.transactionType.upper()
    if txn not in ("BUY", "SELL"):
        raise HTTPException(status_code=400, detail="transactionType must be BUY or SELL")

    tracked = algo._position
    if (tracked and str(tracked.security_id) == body.securityId
            and algo._segment_for(tracked.index_key) == body.exchangeSegment
            and algo.cfg.product_type == body.productType):
        await algo._exit_position_locked(tracked, "MANUAL")
        if algo._position is not None and algo._position.execution_error:
            raise HTTPException(409, algo._position.execution_error)
        return {"ok": True, "dryRun": tracked.dry_run, "status": algo.status()}

    s = get_settings()
    payload: Dict[str, Any] = {
        "dhanClientId": get_dhan_client()._client_id,
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
        "correlationId": f"MAN-X-{uuid.uuid4().hex[:18]}",
    }

    if runtime.is_dry_run():
        audit.log_order(
            action="squareoff", success=True, dry_run=True, status="DRY_RUN",
            message=f"Dry-run square-off {txn} qty={body.quantity}", payload=payload,
        )
        return {"ok": True, "dryRun": True, "status": "DRY_RUN"}

    try:
        client = get_dhan_client()
        key = (body.securityId, body.exchangeSegment, body.productType)
        pending.update(audit.pending_manual_exits())
        if key in pending:
            previous = await client.get_order_by_correlation(pending[key])
            if not isinstance(previous, dict) or not previous.get("orderStatus"):
                raise HTTPException(409, "Previous exit status is unknown. Check this account at Dhan; no repeat exit sent.")
            previous_status = str(previous.get("orderStatus") or "").upper()
            if previous_status not in {"CANCELLED", "REJECTED", "EXPIRED"}:
                raise HTTPException(409, "Previous exit submitted; confirm it at Dhan before sending another.")
            audit.save_manual_exit(key, None)
            del pending[key]
        def matches(row: Dict[str, Any]) -> bool:
            return (str(row.get("securityId")) == body.securityId
                    and row.get("exchangeSegment") == body.exchangeSegment
                    and row.get("productType") == body.productType)
        supers = await client.get_super_orders(fresh=True)
        active = [o for o in supers if matches(o)
                  and str(o.get("orderStatus") or "").upper() not in {"CLOSED", "CANCELLED", "REJECTED", "EXPIRED"}]
        if any(str(o.get("orderStatus") or "").upper() in {"TRANSIT", "PENDING", "PART_TRADED"} for o in active):
            raise HTTPException(409, "Cancel the unfilled Super Order entry before exiting this contract.")
        for order in active:
            for leg in order.get("legDetails") or []:
                if (leg.get("legName") in {"TARGET_LEG", "STOP_LOSS_LEG"}
                        and str(leg.get("orderStatus") or "").upper() not in TERMINAL_ORDERS):
                    await client.cancel_super_order(str(order["orderId"]), leg["legName"])
        if active:
            refreshed = await client.get_super_orders(fresh=True)
            active_ids = {str(o["orderId"]) for o in active}
            if any(str(o.get("orderId")) in active_ids and any(
                leg.get("legName") in {"TARGET_LEG", "STOP_LOSS_LEG"} and (
                    str(leg.get("orderStatus") or "").upper() == "TRADED"
                    or int(leg.get("filledQty") or 0) > 0)
                for leg in o.get("legDetails") or []
            ) for o in refreshed):
                raise HTTPException(409, "Super Order exit filled during cancellation. Wait for holdings sync; no second exit sent.")
            if any(matches(o) and any(
                leg.get("legName") in {"TARGET_LEG", "STOP_LOSS_LEG"}
                and str(leg.get("orderStatus") or "").upper() not in TERMINAL_ORDERS
                for leg in o.get("legDetails") or []
            ) for o in refreshed):
                raise HTTPException(409, "Waiting for target/SL cancellation confirmation. No duplicate exit sent; retry after sync.")
        book = await client.get_order_book(fresh=True)
        if any(matches(o) and o.get("transactionType") == txn
               and str(o.get("orderStatus") or "").upper() not in TERMINAL_ORDERS for o in book):
            raise HTTPException(409, "An exit-side order is already pending at Dhan. Wait for broker sync.")
        rows = await client.get_positions(fresh=True)
        held = sum(int(r.get("netQty") or 0) for r in rows if matches(r))
        if held == 0:
            raise HTTPException(409, "Broker position is already closed. Refresh positions.")
        expected_txn = "SELL" if held > 0 else "BUY"
        if txn != expected_txn or body.quantity > abs(held):
            raise HTTPException(409, "Requested exit no longer matches broker quantity/direction. Refresh positions.")
        audit.save_manual_exit(key, payload["correlationId"])
        pending[key] = payload["correlationId"]
        try:
            result = await client.place_order(payload)
        except DhanAPIError as exc:
            if 0 < exc.status_code < 500:
                audit.save_manual_exit(key, None)
                pending.pop(key, None)
            raise
    except sqlite3.Error as exc:
        raise HTTPException(503, "Cannot persist/recover manual exit identity. Check Dhan before retrying.") from exc
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
