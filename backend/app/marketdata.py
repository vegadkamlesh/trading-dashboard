"""Daily-OHLC cache for index history (shared, long-lived).

Why this exists
---------------
Daily candles change at most once per trading day, yet the guidance / recommender
/ seasonality engines all need them. Instead of re-hitting Dhan's
`/charts/historical` for every request (slow + counts against the rate limit),
we fetch the FULL history (up to ~5 years) once, cache it for hours, and let all
callers slice the tail they need.

Warm-up
-------
`warm_up()` is called at login so the very first prediction is fast and the
expensive historical pull happens exactly once in the background — not on every
20-second signal refresh.

Everything degrades gracefully: on any error we cache an empty series with a
short negative-TTL so we retry soon but never hammer the API.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from .config import get_settings
from .dhan_client import DhanAPIError, get_dhan_client
from .instruments import INDEX_REGISTRY, IndexInstrument

logger = logging.getLogger("dhan.marketdata")

# Daily data is stable intra-day → a 6h TTL is plenty and keeps us light.
DEFAULT_TTL_SECONDS = 6 * 3600
# Failed fetches are retried after this many seconds (negative cache).
ERROR_TTL_SECONDS = 60
# Default window: ~5 years of daily candles (covers seasonality + indicators).
DEFAULT_LOOKBACK_DAYS = 1900
# Dhan caps each /charts/historical range; chunk requests to stay within it.
CHUNK_DAYS = 350
EMPTY: Dict[str, List[float]] = {
    "open": [],
    "high": [],
    "low": [],
    "close": [],
    "timestamp": [],
}


@dataclass
class _Entry:
    series: Dict[str, List[float]]
    fetched_at: float
    ok: bool
    error: Optional[str] = None


_CACHE: Dict[str, _Entry] = {}
_LOCKS: Dict[str, asyncio.Lock] = {}
_CACHE_LOCK = asyncio.Lock()


def _key(inst_key: str) -> str:
    return inst_key.upper()


def _is_fresh(entry: _Entry) -> bool:
    ttl = DEFAULT_TTL_SECONDS if entry.ok else ERROR_TTL_SECONDS
    return (time.time() - entry.fetched_at) < ttl


def _tail(series: Dict[str, List[float]], days: int) -> Dict[str, List[float]]:
    """Return the most recent `days` entries of every column (aligned)."""
    closes = series.get("close") or []
    if not closes or days >= len(closes):
        return series
    out: Dict[str, List[float]] = {}
    for k, v in series.items():
        out[k] = v[-days:] if len(v) >= days else v
    return out


async def _lock_for(key: str) -> asyncio.Lock:
    async with _CACHE_LOCK:
        lk = _LOCKS.get(key)
        if lk is None:
            lk = asyncio.Lock()
            _LOCKS[key] = lk
        return lk


async def get_daily_ohlc(
    inst: IndexInstrument,
    days: int = DEFAULT_LOOKBACK_DAYS,
    *,
    force: bool = False,
) -> Dict[str, List[float]]:
    """Return cached daily OHLC for an index (sliced to the last `days`).

    We always fetch + cache the FULL 5-year window once, then serve any shorter
    request from that cached series — so guidance, recommender and seasonality
    share a single underlying fetch. Concurrency-safe.
    """
    key = _key(inst.key)
    entry = _CACHE.get(key)
    if entry is None or force or not _is_fresh(entry):
        lock = await _lock_for(key)
        async with lock:
            entry = _CACHE.get(key)
            if entry is None or force or not _is_fresh(entry):
                series, err = await _fetch_all(inst)
                entry = _Entry(
                    series=series,
                    fetched_at=time.time(),
                    ok=bool(series.get("close")),
                    error=err,
                )
                _CACHE[key] = entry
                if err:
                    logger.info("daily OHLC for %s unavailable: %s", inst.key, err)
    return _tail(entry.series, days)


async def _fetch_all(
    inst: IndexInstrument, days: int = DEFAULT_LOOKBACK_DAYS
) -> tuple[Dict[str, List[float]], Optional[str]]:
    """Fetch the full window in chunks (Dhan caps each /charts/historical range)."""
    client = get_dhan_client()
    today = date.today()
    start = today - timedelta(days=days)

    acc: Dict[str, List[Any]] = {k: [] for k in EMPTY}
    err: Optional[str] = None
    cursor = start
    while cursor < today:
        end = min(cursor + timedelta(days=CHUNK_DAYS), today)
        try:
            raw = await client.get_historical_daily(
                security_id=str(inst.sec_id),
                exchange_segment="IDX_I",
                instrument="INDEX",
                from_date=cursor.strftime("%Y-%m-%d"),
                to_date=end.strftime("%Y-%m-%d"),
            )
            if isinstance(raw, dict):
                for k in acc:
                    series = raw.get(k)
                    if isinstance(series, list):
                        acc[k].extend(series)
        except DhanAPIError as exc:
            err = exc.error_message
            logger.info("history chunk failed for %s: %s", inst.key, exc)
        except Exception as exc:  # pragma: no cover - network best effort
            err = str(exc)
        cursor = end + timedelta(days=1)

    # Coerce numeric columns, keep timestamps as-is (ints).
    out: Dict[str, List[float]] = {}
    for k, v in acc.items():
        if k == "timestamp":
            out[k] = [int(x) for x in v if isinstance(x, (int, float))]
        else:
            out[k] = [float(x) for x in v if isinstance(x, (int, float))]
    return out, (err if not out.get("close") else None)


async def _warm_history_studies(inst: IndexInstrument) -> None:
    """Prime the (expensive) 5-yr expired-option studies for CALL + PUT ATM.

    These power the recommender's history win-rate and the advisor. Fetching them
    at login means the first prediction doesn't pay the cost. Best-effort.
    """
    from .history import history_engine

    for opt in ("CALL", "PUT"):
        try:
            await history_engine.study(inst, opt, "ATM", lookback_days=90)
        except Exception as exc:  # pragma: no cover - best effort
            logger.info("history warm-up (%s/%s) failed: %s", inst.key, opt, exc)


# History studies are the HEAVIEST warm-up item (each hits the 1.1s heavy gate).
# We only prime the indices a user actually opens, and we do it as a BACKGROUND
# task so login + the first recommend call are never stuck waiting on it.
_HISTORY_WARM_INDICES = ("NIFTY", "SENSEX")


async def warm_up(
    days: int = DEFAULT_LOOKBACK_DAYS, keys: Optional[List[str]] = None
) -> Dict[str, bool]:
    """Pre-fetch daily OHLC for the indices we poll (fast, shared by all panels).

    Called at login so the first prediction is instant. Runs sequentially through
    the throttled client so we never burst the API. Safe to call multiple times
    (cached). Returns index -> success.

    NOTE: the heavy 5-yr history studies are primed SEPARATELY in the background
    (see `prime_history_studies`) so they don't delay login/recommend.
    """
    wanted = {k.upper() for k in (keys or get_settings().oc_polled_indices)}
    targets = [i for i in INDEX_REGISTRY.values() if i.key.upper() in wanted] or list(
        INDEX_REGISTRY.values()
    )
    results: Dict[str, bool] = {}
    for inst in targets:
        try:
            series = await get_daily_ohlc(inst, days=days)
            results[inst.key] = bool(series.get("close"))
        except Exception as exc:  # pragma: no cover
            logger.warning("warm_up failed for %s: %s", inst.key, exc)
            results[inst.key] = False
    logger.info("marketdata warm-up complete: %s", results)
    return results


async def prime_history_studies(keys: Optional[List[str]] = None) -> None:
    """Prime the heavy 5-yr expired-option studies in the BACKGROUND.

    Only NIFTY + SENSEX by default (what users actually open). Runs sequentially
    so it shares the heavy-call gate politely with live requests. Best-effort.
    """
    wanted = {k.upper() for k in (keys or _HISTORY_WARM_INDICES)}
    targets = [i for i in INDEX_REGISTRY.values() if i.key.upper() in wanted]
    for inst in targets:
        try:
            await _warm_history_studies(inst)
        except Exception as exc:  # pragma: no cover - best effort
            logger.info("history study warm-up failed for %s: %s", inst.key, exc)
    logger.info("history studies primed for: %s", [t.key for t in targets])


def cache_state() -> Dict[str, Any]:
    """Lightweight cache introspection for the Connection/Settings panel."""
    now = time.time()
    out = {}
    for key, e in _CACHE.items():
        out[key] = {
            "candles": len(e.series.get("close", [])),
            "ageSeconds": round(now - e.fetched_at, 1),
            "fresh": _is_fresh(e),
            "error": e.error,
        }
    return out
