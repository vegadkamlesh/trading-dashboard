"""5-year expired-options STUDY engine.

What it does
------------
Dhan's `/charts/rollingoption` endpoint gives minute-level, strike-wise data for
EXPIRED option contracts, up to 5 years back (last 30 days per call). We use it
to answer questions like:

  "In the last N expiries, if you had bought the ATM CE at ~9:20 and sold at
   ~15:15, how often did it go up, and by how much on average?"

These empirical stats feed the guidance engine so predictions are grounded in
what actually happened, not just live indicators.

Design
------
* Fetching is EXPENSIVE (large payloads), so results are CACHED in memory with a
  long TTL (default 12h) and refreshed only on explicit request.
* We sample a handful of recent expiries (configurable) rather than all 5 years
  by default, to stay friendly to the free data tier. Increase `lookback_days`
  to widen the study.
* Everything degrades gracefully: on any error we return a partial/empty study
  with an `error` message instead of raising.

This module is deliberately dependent on `dhan_client` only (no FastAPI imports)
so it is unit-testable.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from .dhan_client import DhanAPIError, get_dhan_client
from .instruments import IndexInstrument

logger = logging.getLogger("dhan.history")

DEFAULT_TTL_SECONDS = 12 * 3600
MAX_DAYS_PER_CALL = 30  # Dhan limit for rollingoption
MAX_STUDY_DAYS = 180    # hard cap so a single refresh can't hammer the API


@dataclass
class ExpiryStudy:
    """Empirical outcome stats for one (index, optionType, strikeOffset)."""

    index_key: str
    option_type: str          # CALL | PUT (Dhan naming)
    strike_offset: str        # ATM, ATM+1, ATM-1 ...
    contract_win_rate: Optional[float]       # % of sampled days the option ended green
    avg_intraday_gain_pct: Optional[float]   # avg best-case move from open
    avg_intraday_drawdown_pct: Optional[float]  # avg worst-case move from open
    avg_close_vs_open_pct: Optional[float]
    sample_days: int
    iv_avg: Optional[float]
    spot_move_avg_pct: Optional[float]       # avg |spot open->close| move
    fetched_at: float = field(default_factory=time.time)
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index_key,
            "optionType": self.option_type,
            "strikeOffset": self.strike_offset,
            "winRate": self.contract_win_rate,
            "avgIntradayGainPct": self.avg_intraday_gain_pct,
            "avgIntradayDrawdownPct": self.avg_intraday_drawdown_pct,
            "avgCloseVsOpenPct": self.avg_close_vs_open_pct,
            "sampleDays": self.sample_days,
            "ivAvg": self.iv_avg,
            "spotMoveAvgPct": self.spot_move_avg_pct,
            "ageSeconds": round(time.time() - self.fetched_at, 1),
            "error": self.error,
        }


def _to_epoch(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, 9, 20).timestamp())


def _safe_list(d: Dict[str, Any], key: str) -> List[Any]:
    v = d.get(key)
    return v if isinstance(v, list) else []


class HistoryEngine:
    """In-memory cache of expired-options studies."""

    def __init__(self) -> None:
        self._cache: Dict[str, Tuple[float, ExpiryStudy]] = {}

    @staticmethod
    def _cache_key(index_key: str, option_type: str, strike_offset: str, days: int) -> str:
        return f"{index_key.upper()}|{option_type.upper()}|{strike_offset.upper()}|{days}"

    def cached(
        self, index_key: str, option_type: str, strike_offset: str, days: int
    ) -> Optional[ExpiryStudy]:
        key = self._cache_key(index_key, option_type, strike_offset, days)
        entry = self._cache.get(key)
        if not entry:
            return None
        ts, study = entry
        if time.time() - ts > DEFAULT_TTL_SECONDS:
            return None
        return study

    async def study(
        self,
        inst: IndexInstrument,
        option_type: str,
        strike_offset: str = "ATM",
        *,
        lookback_days: int = 90,
        expiry_flag: str = "WEEK",
        force: bool = False,
    ) -> ExpiryStudy:
        """Return (cached) empirical stats for an index/option strike.

        `option_type` is 'CALL' or 'PUT'. `lookback_days` bounded by MAX_STUDY_DAYS.
        """
        option_type = option_type.upper()
        strike_offset = strike_offset.upper()
        lookback_days = max(7, min(int(lookback_days), MAX_STUDY_DAYS))

        if not force:
            hit = self.cached(inst.key, option_type, strike_offset, lookback_days)
            if hit is not None:
                return hit

        study = await self._compute(
            inst, option_type, strike_offset, lookback_days, expiry_flag
        )
        key = self._cache_key(inst.key, option_type, strike_offset, lookback_days)
        self._cache[key] = (time.time(), study)
        return study

    async def _compute(
        self,
        inst: IndexInstrument,
        option_type: str,
        strike_offset: str,
        lookback_days: int,
        expiry_flag: str,
    ) -> ExpiryStudy:
        client = get_dhan_client()
        seg = "NSE_FNO" if inst.underlying_seg == "IDX_I" and inst.key in (
            "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"
        ) else "BSE_FNO"

        today = date.today()
        # Chunk the window into <=30-day calls (Dhan limit).
        chunks: List[Tuple[date, date]] = []
        cursor = today - timedelta(days=lookback_days)
        while cursor < today:
            end = min(cursor + timedelta(days=MAX_DAYS_PER_CALL), today)
            chunks.append((cursor, end))
            cursor = end

        all_close_vs_open: List[float] = []
        all_gain: List[float] = []
        all_drawdown: List[float] = []
        all_iv: List[float] = []
        spot_moves: List[float] = []
        wins = 0
        total = 0
        err: Optional[str] = None

        for start, end in chunks[-3:]:  # limit to last ~90 days worth of chunks
            try:
                raw = await client.get_rolling_expired_options(
                    security_id=str(inst.sec_id),
                    exchange_segment=seg,
                    instrument="OPTIDX",
                    interval="60",
                    expiry_flag=expiry_flag,
                    expiry_code=0,
                    strike=strike_offset,
                    drv_option_type=option_type,
                    required_data=["open", "high", "low", "close", "iv", "spot", "volume"],
                    from_date=start.strftime("%Y-%m-%d"),
                    to_date=end.strftime("%Y-%m-%d"),
                )
            except DhanAPIError as exc:
                err = exc.error_message
                logger.warning("rollingoption failed: %s", exc)
                continue

            data = (raw or {}).get("data") or {}
            leg = data.get("ce") if option_type == "CALL" else data.get("pe")
            if not leg:
                continue
            self._accumulate(
                leg, all_close_vs_open, all_gain, all_drawdown, all_iv, spot_moves
            )

        # Win rate: fraction of sampled sessions where close-vs-open was positive.
        wins = sum(1 for x in all_close_vs_open if x > 0)
        total = len(all_close_vs_open)

        return ExpiryStudy(
            index_key=inst.key,
            option_type=option_type,
            strike_offset=strike_offset,
            contract_win_rate=round(wins / total * 100, 1) if total else None,
            avg_intraday_gain_pct=_avg(all_gain),
            avg_intraday_drawdown_pct=_avg(all_drawdown),
            avg_close_vs_open_pct=_avg(all_close_vs_open),
            sample_days=total,
            iv_avg=_avg(all_iv),
            spot_move_avg_pct=_avg(spot_moves),
            error=err,
        )

    @staticmethod
    def _accumulate(
        leg: Dict[str, Any],
        close_vs_open: List[float],
        gains: List[float],
        drawdowns: List[float],
        ivs: List[float],
        spot_moves: List[float],
    ) -> None:
        opens = _safe_list(leg, "open")
        highs = _safe_list(leg, "high")
        lows = _safe_list(leg, "low")
        closes = _safe_list(leg, "close")
        ivs_raw = _safe_list(leg, "iv")
        spots = _safe_list(leg, "spot")
        stamps = _safe_list(leg, "timestamp")

        n = min(len(opens), len(highs), len(lows), len(closes))
        if n == 0:
            return

        # Group minute rows by calendar day, then summarise each day.
        day_buckets: Dict[int, List[int]] = {}
        for i in range(n):
            if i < len(stamps):
                day_key = int(stamps[i]) // 86400
            else:
                day_key = 0
            day_buckets.setdefault(day_key, []).append(i)

        for idxs in day_buckets.values():
            if len(idxs) < 2:
                continue
            o = _f(opens[idxs[0]])
            c = _f(closes[idxs[-1]])
            hi = max(_f(highs[i]) for i in idxs)
            lo = min(_f(lows[i]) for i in idxs)
            if o and o > 0:
                close_vs_open.append(round((c - o) / o * 100, 2))
                if hi and hi > 0:
                    gains.append(round((hi - o) / o * 100, 2))
                if lo and lo > 0:
                    drawdowns.append(round((lo - o) / o * 100, 2))
            # Spot move for the day
            if spots:
                s_open = _f(spots[idxs[0]])
                s_close = _f(spots[idxs[-1]])
                if s_open and s_open > 0 and s_close:
                    spot_moves.append(abs((s_close - s_open) / s_open * 100))

        for v in ivs_raw:
            fv = _f(v)
            if fv:
                ivs.append(fv)


def _f(v: Any) -> Optional[float]:
    try:
        f = float(v)
        return f
    except (TypeError, ValueError):
        return None


def _avg(xs: List[float]) -> Optional[float]:
    if not xs:
        return None
    return round(sum(xs) / len(xs), 2)


history_engine = HistoryEngine()
