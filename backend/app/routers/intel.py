"""Intelligence endpoints — guidance, 5-yr history studies, news/pulse.

These are heavier than the market cache, so:
  * history studies are cached server-side (long TTL) and only refetch on
    `?refresh=1`,
  * news only refetches on `?refresh=1`,
  * guidance reads the warm option-chain cache + (cached) history.

All endpoints require a session. None are polled aggressively by the UI.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from .. import guidance as guidance_engine
from .. import marketdata
from ..config import get_settings
from ..history import history_engine
from ..instruments import get_index
from ..news import fetch_news
from ..optionchain import engine as oc_engine
from ..recommender import recommend as recommend_engine
from ..seasonality import seasonality_engine
from ..security import limit, require_session

router = APIRouter(prefix="/api/intel", tags=["intel"])


@router.get("/guidance")
async def guidance(
    index: str = Query(..., description="Index key e.g. NIFTY"),
    optionType: str = Query("CE", description="CE or PE"),
    useHistory: bool = Query(True),
    historyDays: int = Query(90, ge=7, le=180),
    _session: str = Depends(require_session),
):
    """Explainable BUY/SELL/WAIT verdict with reasons + projected scenarios."""
    key = index.upper()
    if key not in _INDEX_KEYS():
        raise HTTPException(status_code=404, detail="Unknown index")
    limit(f"guidance:{key}", max_calls=20, window_seconds=60.0, message="Slow down")
    result = await guidance_engine.advise(
        key, optionType, use_history=useHistory, history_days=historyDays
    )
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.get("/history")
async def history(
    index: str = Query(...),
    optionType: str = Query("CE"),
    strikeOffset: str = Query("ATM", description="ATM, ATM+1, ATM-1 ..."),
    lookbackDays: int = Query(90, ge=7, le=180),
    refresh: bool = Query(False),
    _session: str = Depends(require_session),
):
    """Empirical stats from the last N days of EXPIRED option data."""
    key = index.upper()
    inst = get_index(key)
    if inst is None:
        raise HTTPException(status_code=404, detail="Unknown index")
    opt = "CALL" if optionType.upper() == "CE" else "PUT"
    limit(f"history:{key}", max_calls=6, window_seconds=60.0, message="Slow down")
    study = await history_engine.study(
        inst, opt, strikeOffset, lookback_days=lookbackDays, force=refresh
    )
    return study.to_dict()


@router.get("/news")
async def news(
    refresh: bool = Query(False),
    _session: str = Depends(require_session),
):
    """Market headlines (free RSS). Only refetches when refresh=1 or stale."""
    limit("news", max_calls=12, window_seconds=60.0, message="Slow down")
    feeds = _news_feeds()
    return await fetch_news(force=refresh, feeds=feeds or None)


@router.get("/seasonality")
async def seasonality(
    index: str = Query(...),
    years: int = Query(5, ge=1, le=5),
    weekdayLookback: int = Query(100, ge=10, le=260),
    _session: str = Depends(require_session),
):
    """What happened on this date / weekday in past years (seasonality)."""
    key = index.upper()
    inst = get_index(key)
    if inst is None:
        raise HTTPException(status_code=404, detail="Unknown index")
    limit(f"season:{key}", max_calls=10, window_seconds=60.0, message="Slow down")
    result = await seasonality_engine.analyze(
        inst, years=years, weekday_lookback=weekdayLookback
    )
    return result.to_dict()


@router.get("/recommend")
async def recommend(
    index: str = Query(...),
    rangeStrikes: int = Query(10, ge=3, le=15),
    _session: str = Depends(require_session),
):
    """Per-strike BUY recommendations near the money, with theta awareness."""
    key = index.upper()
    inst = get_index(key)
    if inst is None:
        raise HTTPException(status_code=404, detail="Unknown index")
    snap = oc_engine.snapshot(key)
    if not snap or not snap.get("rows"):
        raise HTTPException(status_code=503, detail="Option chain warming up, retry soon")
    limit(f"rec:{key}", max_calls=20, window_seconds=60.0, message="Slow down")

    # Shared, long-TTL daily-OHLC cache (warmed at login) → no per-refresh fetch.
    ohlc = await marketdata.get_daily_ohlc(inst, days=180)
    # History win-rates — CACHE-ONLY so this hot path never blocks on the heavy
    # 5-yr rollingoption fetch. The background warm-up primes ATM studies; until
    # then we simply proceed without the history weight (neutral).
    ce_win = pe_win = None
    try:
        ce = await history_engine.study(
            inst, "CALL", "ATM", lookback_days=90, cached_only=True
        )
        pe = await history_engine.study(
            inst, "PUT", "ATM", lookback_days=90, cached_only=True
        )
        ce_win = ce.contract_win_rate
        pe_win = pe.contract_win_rate
    except Exception:  # pragma: no cover - history is best-effort
        pass

    # Latest news sentiment (cached — does NOT force a feed refresh here).
    news_score = None
    try:
        news_data = await fetch_news(force=False, feeds=_news_feeds() or None)
        summary = (news_data or {}).get("summary") or {}
        news_score = summary.get("netScore")
    except Exception:  # pragma: no cover - news is best-effort
        pass

    return recommend_engine(
        index_key=key,
        expiry=snap.get("expiry", ""),
        spot=snap.get("underlyingLtp", 0.0),
        rows=snap.get("rows", []),
        closes=ohlc["close"],
        highs=ohlc["high"],
        lows=ohlc["low"],
        hist_win_rate_ce=ce_win,
        hist_win_rate_pe=pe_win,
        news_net_score=news_score,
        range_strikes=rangeStrikes,
    )


def _news_feeds():
    raw = getattr(get_settings(), "app_news_feeds", "") or ""
    feeds = []
    for pair in raw.split(";"):
        if "|" in pair:
            name, url = pair.split("|", 1)
            if name.strip() and url.strip():
                feeds.append((name.strip(), url.strip()))
    return feeds


def _INDEX_KEYS():
    from ..instruments import INDEX_REGISTRY

    return set(INDEX_REGISTRY.keys())
