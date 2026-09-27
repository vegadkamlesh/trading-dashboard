"""Algo (ORB auto-trader) endpoints.

Read endpoints are cheap (in-memory) and safe to poll frequently.
Write endpoints require the session + CSRF, exactly like the manual order API.

Nothing here can place an order by itself: the algo only trades while it is
ARMED, and it respects the global DRY-RUN switch.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from ..algo import algo
from ..algo_store import store
from ..orb_study import study
from ..security import require_csrf, require_session

router = APIRouter(prefix="/api/algo", tags=["algo"])


class ConfigPatch(BaseModel):
    sl_mult: Optional[float] = None
    target_mult: Optional[float] = None
    breakeven_at: Optional[float] = None
    entry_cutoff: Optional[str] = None
    exit_time: Optional[str] = None
    strike_offset: Optional[int] = None
    risk_pct: Optional[float] = None
    max_capital_pct: Optional[float] = None
    max_lots: Optional[int] = None
    min_lots: Optional[int] = None
    min_or_pct: Optional[float] = None
    max_or_pct: Optional[float] = None
    order_type: Optional[str] = None
    product_type: Optional[str] = None
    protective_sl: Optional[bool] = None
    protective_sl_mult: Optional[float] = None
    indices: Optional[List[str]] = None


@router.get("/status")
async def status(_session: str = Depends(require_session)) -> Dict[str, Any]:
    return algo.status()


@router.post("/arm")
async def arm(_session: str = Depends(require_session), _csrf: None = Depends(require_csrf)):
    return await algo.arm()


@router.post("/disarm")
async def disarm(_session: str = Depends(require_session), _csrf: None = Depends(require_csrf)):
    return algo.disarm("operator")


@router.post("/exit")
async def exit_now(_session: str = Depends(require_session), _csrf: None = Depends(require_csrf)):
    """Panic square-off of the algo's open position."""
    return await algo.panic_exit("PANIC button")


@router.post("/reconcile")
async def reconcile(_session: str = Depends(require_session), _csrf: None = Depends(require_csrf)):
    result = await algo.reconcile()
    return {"ok": True, "reconcile": result, "status": algo.status()}


@router.post("/config")
async def config(
    patch: ConfigPatch,
    _session: str = Depends(require_session),
    _csrf: None = Depends(require_csrf),
):
    return algo.set_config(patch.model_dump(exclude_none=True))


@router.get("/journal")
async def journal(
    days: int = Query(7, ge=1, le=30),
    _session: str = Depends(require_session),
) -> Dict[str, Any]:
    return {
        "days": store.journal_days()[:days],
        "events": store.read_journal(days=days),
        "keepDays": 7,
    }


@router.get("/trades")
async def trades(
    days: int = Query(7, ge=1, le=365),
    _session: str = Depends(require_session),
) -> Dict[str, Any]:
    rows = store.recent_trades(days=days)
    wins = [t for t in rows if float(t.get("premiumPnl") or 0) > 0]
    return {
        "trades": rows,
        "summary": {
            "count": len(rows),
            "wins": len(wins),
            "winRate": round(len(wins) / len(rows) * 100, 1) if rows else 0.0,
            "netRupees": round(sum(float(t.get("premiumPnl") or 0) for t in rows), 0),
            "netR": round(sum(float(t.get("rMultiple") or 0) for t in rows), 2),
        },
    }


@router.get("/eod")
async def eod(
    day: Optional[str] = None,
    _session: str = Depends(require_session),
) -> Dict[str, Any]:
    """Everything the algo did on a given day (defaults to today)."""
    target = day or date.today().isoformat()
    events = store.read_day(target)
    trades = [t for t in store.read_trades(limit=5000) if str(t.get("date")) == target]
    orders = [e for e in events if e.get("kind") == "algo"]
    problems = [e for e in events if e.get("level") in ("error", "warn")]
    return {
        "date": target,
        "availableDays": store.journal_days(),
        "events": events,
        "trades": trades,
        "summary": {
            "events": len(events),
            "trades": len(trades),
            "wins": len([t for t in trades if float(t.get("premiumPnl") or 0) > 0]),
            "netRupees": round(sum(float(t.get("premiumPnl") or 0) for t in trades), 0),
            "netR": round(sum(float(t.get("rMultiple") or 0) for t in trades), 2),
            "warnings": len(problems),
        },
    }


@router.get("/equity")
async def equity(
    days: int = Query(90, ge=7, le=1000),
    _session: str = Depends(require_session),
) -> Dict[str, Any]:
    """Cumulative P&L curve (rupees) from the trade log, for the chart."""
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    rows = [t for t in store.read_trades(limit=5000) if str(t.get("date")) >= cutoff]
    rows.sort(key=lambda t: (str(t.get("date")), str(t.get("exitTime"))))
    cum = 0.0
    points = []
    for t in rows:
        cum += float(t.get("premiumPnl") or 0)
        points.append({"date": t.get("date"), "time": t.get("exitTime"),
                       "cumulative": round(cum, 0),
                       "trade": round(float(t.get("premiumPnl") or 0), 0)})
    return {"points": points, "net": round(cum, 0)}


# ---------------------------------------------------------------- study ----
@router.get("/study")
async def study_status(_session: str = Depends(require_session)) -> Dict[str, Any]:
    return study.status()


@router.post("/study/start")
async def study_start(
    years: int = Query(5, ge=1, le=10),
    _session: str = Depends(require_session),
    _csrf: None = Depends(require_csrf),
):
    return study.start(years=years)
