"""Algo 2 endpoints (/api/algo2/*).

Reads are in-memory and cheap to poll. Writes need the session + CSRF.
Algo 2 has its OWN ON/OFF, Paper/Live mode and account selection; none of
these touch Algo 1.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..accounts import ACCOUNT_IDS, account_scope
from ..algo2.engine import group
from ..security import require_csrf, require_session

router = APIRouter(prefix="/api/algo2", tags=["algo2"])


class Toggle(BaseModel):
    enabled: bool


class Mode(BaseModel):
    dry_run: bool


class Accounts(BaseModel):
    accounts: List[str]


class ConfigPatch(BaseModel):
    indices: Optional[List[str]] = None
    entry_cutoff: Optional[str] = None
    exit_time: Optional[str] = None
    max_trades: Optional[int] = None
    max_lots: Optional[int] = None
    min_lots: Optional[int] = None
    max_affordable: Optional[bool] = None
    risk_pct: Optional[float] = None
    max_capital_pct: Optional[float] = None
    protective_sl: Optional[bool] = None
    protective_sl_mult: Optional[float] = None
    profit_target_rs: Optional[float] = None
    stop_loss_rs: Optional[float] = None
    conditions: Optional[Dict[str, bool]] = None
    trade_windows: Optional[str] = None
    entry_buffer: Optional[Dict[str, float]] = None
    max_stop_loss_pct: Optional[float] = None
    capital_sl_cap: Optional[bool] = None
    stop_after_sl: Optional[bool] = None
    status_report_minutes: Optional[int] = None
    max_hold_minutes: Optional[int] = None
    oi_velocity_spike: Optional[float] = None
    min_score: Optional[int] = None
    data_sl: Optional[bool] = None
    data_sl_gap_pct: Optional[float] = None
    event_window_minutes: Optional[int] = None
    target_delta: Optional[float] = None
    partial_booking: Optional[bool] = None
    t1_pct: Optional[float] = None
    pcr_chop_low: Optional[float] = None
    pcr_chop_high: Optional[float] = None
    vix_spike_pct: Optional[float] = None
    oi_strikes: Optional[int] = None
    vwap_band_sd: Optional[float] = None
    profile_bin: Optional[Dict[str, float]] = None
    nifty_weights: Optional[Dict[str, float]] = None
    sensex_weights: Optional[Dict[str, float]] = None


class EventIn(BaseModel):
    date: str
    time: Optional[str] = None
    name: str
    impact: Optional[str] = "high"


@router.get("/status")
async def status(_session: str = Depends(require_session)) -> Dict[str, Any]:
    return group.status()


@router.post("/enabled")
async def set_enabled(body: Toggle, _session: str = Depends(require_session),
                      _csrf: None = Depends(require_csrf)) -> Dict[str, Any]:
    return await group.set_enabled(body.enabled)


@router.post("/mode")
async def set_mode(body: Mode, _session: str = Depends(require_session),
                   _csrf: None = Depends(require_csrf)) -> Dict[str, Any]:
    return group.set_mode(body.dry_run)


@router.post("/accounts")
async def set_accounts(body: Accounts, _session: str = Depends(require_session),
                       _csrf: None = Depends(require_csrf)) -> Dict[str, Any]:
    return group.set_accounts(body.accounts)


@router.post("/config")
async def set_config(patch: ConfigPatch, _session: str = Depends(require_session),
                     _csrf: None = Depends(require_csrf)) -> Dict[str, Any]:
    return group.set_config(patch.model_dump(exclude_none=True))


@router.post("/exit")
async def panic_exit(_session: str = Depends(require_session),
                     _csrf: None = Depends(require_csrf)) -> Dict[str, Any]:
    return await group.panic_exit()


@router.post("/reconcile")
async def reconcile(_session: str = Depends(require_session),
                    _csrf: None = Depends(require_csrf)) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for account, engine in group.items.items():
        with account_scope(account):
            out[account] = await engine.reconcile()
    return {"ok": True, "reconcile": out, "status": group.status()}


@router.post("/events")
async def add_event(body: EventIn, _session: str = Depends(require_session),
                    _csrf: None = Depends(require_csrf)) -> Dict[str, Any]:
    try:
        event = group.news.add(body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"event": event, "status": group.status()}


@router.post("/events/{event_id}/delete")
async def remove_event(event_id: str, _session: str = Depends(require_session),
                       _csrf: None = Depends(require_csrf)) -> Dict[str, Any]:
    if not group.news.remove(event_id):
        raise HTTPException(404, "Event not found (NSE events cannot be removed)")
    return group.status()


def _store(account: str):
    if account not in ACCOUNT_IDS:
        raise HTTPException(400, "account must be master or child")
    return group.items[account]._store


@router.get("/journal")
async def journal(account: str = Query("master"), days: int = Query(7, ge=1, le=30),
                  _session: str = Depends(require_session)) -> Dict[str, Any]:
    store = _store(account)
    return {"days": store.journal_days()[:days], "events": store.read_journal(days=days), "keepDays": None}


@router.get("/trades")
async def trades(account: str = Query("master"), days: int = Query(30, ge=1, le=365),
                 _session: str = Depends(require_session)) -> Dict[str, Any]:
    rows = _store(account).recent_trades(days=days)
    wins = [t for t in rows if float(t.get("premiumPnl") or 0) > 0]
    return {
        "trades": rows,
        "summary": {
            "count": len(rows), "wins": len(wins),
            "winRate": round(len(wins) / len(rows) * 100, 1) if rows else 0.0,
            "netRupees": round(sum(float(t.get("premiumPnl") or 0) for t in rows), 0),
        },
    }
