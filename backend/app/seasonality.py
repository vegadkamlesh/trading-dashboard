"""Seasonality engine — "what happened on this date / this weekday before?"

Two classic discretionary checks, made quantitative:

  1. SAME-DATE HISTORICAL  — "Aaj 18 Sep hai. Last 5 saal me 18 Sep ko kya hua?"
     For each of the last N years we look at that calendar date (±0/1 trading
     day) and report the open/close move.

  2. DAY-OF-WEEK (SEASONAL) — "Aaj Monday hai. Last 100 Mondays ko kya hua?"
     We bucket daily closes by weekday and report the average move, win-rate
     and best/worst for the same weekday.

Data source: Dhan `/charts/historical` daily candles for the index. Pulling 5
years is chunked (the API caps the range), parsed once, and cached in memory
with a long TTL — seasonality barely changes intraday, so this is cheap.

Everything degrades gracefully: on error we return a partial result with an
`error` string instead of raising.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from . import marketdata
from .dhan_client import DhanAPIError, get_dhan_client
from .instruments import IndexInstrument

logger = logging.getLogger("dhan.seasonality")

CACHE_TTL_SECONDS = 6 * 3600  # seasonality is stable; refresh every few hours
MAX_YEARS = 5
CHUNK_DAYS = 350  # keep each historical call well within the API window
MAX_DAYS = 365 * MAX_YEARS + 60  # shared-cache window we ask for

_WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]


@dataclass
class DailyBar:
    d: date
    open: float
    high: float
    low: float
    close: float

    @property
    def change_pct(self) -> Optional[float]:
        if self.open:
            return round((self.close - self.open) / self.open * 100, 2)
        return None


@dataclass
class SeasonalityResult:
    index_key: str
    today: str
    weekday: str
    # same-date samples: [{year, date, open, close, changePct}]
    same_date: List[Dict[str, Any]] = field(default_factory=list)
    same_date_avg_pct: Optional[float] = None
    same_date_up_days: int = 0
    same_date_total: int = 0
    # weekday samples across lookback
    weekday_stats: Dict[str, Any] = field(default_factory=dict)
    # overall recent trend context
    bars: int = 0
    fetched_at: float = field(default_factory=time.time)
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index_key,
            "today": self.today,
            "weekday": self.weekday,
            "sameDate": {
                "samples": self.same_date,
                "avgPct": self.same_date_avg_pct,
                "upDays": self.same_date_up_days,
                "total": self.same_date_total,
            },
            "weekdayStats": self.weekday_stats,
            "barsAnalyzed": self.bars,
            "ageSeconds": round(time.time() - self.fetched_at, 1),
            "error": self.error,
        }


class SeasonalityEngine:
    def __init__(self) -> None:
        self._cache: Dict[str, Tuple[float, List[DailyBar]]] = {}

    # ---------- fetching ----------
    async def _load_daily(self, inst: IndexInstrument, years: int = MAX_YEARS) -> List[DailyBar]:
        key = inst.key.upper()
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL_SECONDS:
            return hit[1]

        # Single shared fetch (warmed at login) — no per-request historical call.
        series = await marketdata.get_daily_ohlc(inst, days=MAX_DAYS)
        bars = self._parse_series(series)
        bars.sort(key=lambda b: b.d)
        self._cache[key] = (time.time(), bars)
        return bars

    @staticmethod
    def _parse_series(series: Dict[str, Any]) -> List[DailyBar]:
        if not isinstance(series, dict):
            return []
        opens = series.get("open") or []
        highs = series.get("high") or []
        lows = series.get("low") or []
        closes = series.get("close") or []
        stamps = series.get("timestamp") or []
        n = min(len(opens), len(highs), len(lows), len(closes), len(stamps))
        out: List[DailyBar] = []
        for i in range(n):
            try:
                d = datetime.fromtimestamp(int(stamps[i])).date()
                out.append(
                    DailyBar(
                        d=d,
                        open=float(opens[i]),
                        high=float(highs[i]),
                        low=float(lows[i]),
                        close=float(closes[i]),
                    )
                )
            except (TypeError, ValueError, OverflowError):
                continue
        return out

    # ---------- analysis ----------
    async def analyze(
        self,
        inst: IndexInstrument,
        *,
        years: int = MAX_YEARS,
        weekday_lookback: int = 100,
    ) -> SeasonalityResult:
        today = date.today()
        res = SeasonalityResult(
            index_key=inst.key,
            today=today.isoformat(),
            weekday=today.strftime("%A"),
        )
        try:
            bars = await self._load_daily(inst, years)
        except Exception as exc:  # pragma: no cover - defensive
            res.error = str(exc)
            return res

        if not bars:
            res.error = "no historical data"
            return res

        res.bars = len(bars)
        by_date = {b.d: b for b in bars}

        # On a weekend, the relevant seasonal day is the NEXT trading day
        # (Monday) — that's the day the trader is actually planning for.
        seasonal_weekday = today.weekday()
        if seasonal_weekday >= 5:
            seasonal_weekday = 0  # Monday
            res.weekday = _WEEKDAYS[0]

        # ---- 1. Same-date over the last N years ----
        for y in range(1, years + 1):
            target = self._safe_date(today, y)
            bar = self._nearest(by_date, target, max_drift=3)
            if bar and (today - bar.d).days > 0:
                res.same_date.append(
                    {
                        "year": bar.d.year,
                        "date": bar.d.isoformat(),
                        "open": bar.open,
                        "close": bar.close,
                        "changePct": bar.change_pct,
                    }
                )
        changes = [s["changePct"] for s in res.same_date if s["changePct"] is not None]
        if changes:
            res.same_date_avg_pct = round(sum(changes) / len(changes), 2)
            res.same_date_up_days = sum(1 for c in changes if c > 0)
            res.same_date_total = len(changes)

        # ---- 2. Day-of-week seasonal (e.g. last 100 Mondays) ----
        res.weekday_stats = self._weekday_stats(bars, seasonal_weekday, weekday_lookback)
        return res

    @staticmethod
    def _weekday_stats(bars: List[DailyBar], weekday: int, lookback: int) -> Dict[str, Any]:
        name = _WEEKDAYS[weekday] if 0 <= weekday < 5 else "N/A"
        # bars are oldest->newest; take the most recent `lookback` matching days
        matching = [b for b in bars if b.d.weekday() == weekday]
        matching = matching[-lookback:]
        changes = [b.change_pct for b in matching if b.change_pct is not None]
        if not changes:
            return {"weekday": name, "samples": 0}
        ups = sum(1 for c in changes if c > 0)
        return {
            "weekday": name,
            "samples": len(changes),
            "avgPct": round(sum(changes) / len(changes), 2),
            "winRate": round(ups / len(changes) * 100, 1),
            "bestPct": round(max(changes), 2),
            "worstPct": round(min(changes), 2),
            "lastPct": round(changes[-1], 2),
        }

    @staticmethod
    def _safe_date(today: date, years_back: int) -> date:
        """Same month/day `years_back` years ago (Feb 29 -> Feb 28)."""
        y = today.year - years_back
        try:
            return today.replace(year=y)
        except ValueError:
            return today.replace(year=y, day=28)

    @staticmethod
    def _nearest(
        by_date: Dict[date, DailyBar], target: date, max_drift: int = 3
    ) -> Optional[DailyBar]:
        for drift in range(max_drift + 1):
            for delta in ({0} if drift == 0 else {-drift, drift}):
                bar = by_date.get(target + timedelta(days=delta))
                if bar:
                    return bar
        return None


seasonality_engine = SeasonalityEngine()
