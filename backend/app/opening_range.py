"""Opening-range (first 10 minutes, 09:15-09:25) study engine.

Question it answers
-------------------
"Pichle 5 saal me, har din 9:15 se 9:25 ke beech NIFTY/SENSEX ka HIGH aur LOW
kitna tha?" - plus what typically happened *after* that window.

Why it matters (trading)
------------------------
The 09:15-09:25 range (the "opening range") is a classic reference:
  * it sizes your stop distance (a stop tighter than the opening range gets
    shaken out by noise),
  * ORB (opening-range breakout) traders trade the break of this band,
  * its height is a quick read on how volatile the day is likely to be.

Data source
-----------
Dhan `/charts/intraday` (interval = 1 minute). The API caps each request at 90
days, so a 5-year study is chunked (~21 calls) and CACHED in memory with a long
TTL - the first load may take a few seconds, every later view is instant.

Everything degrades gracefully: on error we return a partial/empty study with an
`error` string instead of raising.
"""
from __future__ import annotations

import asyncio
import logging
import statistics
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from .dhan_client import DhanAPIError, get_dhan_client
from .instruments import IndexInstrument

logger = logging.getLogger("dhan.opening_range")

CACHE_TTL_SECONDS = 6 * 3600
MAX_DAYS_PER_CALL = 90          # Dhan intraday limit
MAX_YEARS = 5
WINDOW_START_MIN = 9 * 60 + 15  # 09:15
WINDOW_END_MIN = 9 * 60 + 25    # 09:25 (inclusive)
RECENT_SAMPLES = 20             # show the most recent N sessions
# Bulk back-fill concurrency. Dhan's data API tolerates ~5 req/s; we stay well
# under it with 3 in flight and retry on DH-904 (rate limit).
FETCH_CONCURRENCY = 3
RATE_LIMIT_RETRIES = 4


@dataclass
class DayRange:
    d: date
    open: float
    or_high: float
    or_low: float
    day_high: float
    day_low: float
    day_close: float

    @property
    def range_pts(self) -> float:
        return round(self.or_high - self.or_low, 2)

    @property
    def range_pct(self) -> Optional[float]:
        if self.open:
            return round((self.or_high - self.or_low) / self.open * 100, 3)
        return None

    @property
    def day_range_pct(self) -> Optional[float]:
        if self.open:
            return round((self.day_high - self.day_low) / self.open * 100, 3)
        return None


@dataclass
class OpeningRangeResult:
    index_key: str
    years: int
    sample_days: int = 0
    avg_range_points: Optional[float] = None
    avg_range_pct: Optional[float] = None
    median_range_pct: Optional[float] = None
    p90_range_pct: Optional[float] = None
    max_range_pct: Optional[float] = None
    min_range_pct: Optional[float] = None
    avg_day_range_pct: Optional[float] = None
    opening_share_of_day_pct: Optional[float] = None
    # Behaviour after the window (% of sampled days)
    break_up_rate: Optional[float] = None      # day high exceeded OR high
    break_down_rate: Optional[float] = None    # day low pierced OR low
    close_above_rate: Optional[float] = None   # closed above OR high
    close_below_rate: Optional[float] = None   # closed below OR low
    close_inside_rate: Optional[float] = None  # closed back inside the band
    range_buckets: List[Dict[str, Any]] = field(default_factory=list)
    recent: List[Dict[str, Any]] = field(default_factory=list)
    fetched_at: float = field(default_factory=time.time)
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index_key,
            "years": self.years,
            "windowStart": "09:15",
            "windowEnd": "09:25",
            "sampleDays": self.sample_days,
            "avgRangePoints": self.avg_range_points,
            "avgRangePct": self.avg_range_pct,
            "medianRangePct": self.median_range_pct,
            "p90RangePct": self.p90_range_pct,
            "maxRangePct": self.max_range_pct,
            "minRangePct": self.min_range_pct,
            "avgDayRangePct": self.avg_day_range_pct,
            "openingShareOfDayPct": self.opening_share_of_day_pct,
            "breakUpRate": self.break_up_rate,
            "breakDownRate": self.break_down_rate,
            "closeAboveRate": self.close_above_rate,
            "closeBelowRate": self.close_below_rate,
            "closeInsideRate": self.close_inside_rate,
            "rangeBuckets": self.range_buckets,
            "recent": self.recent,
            "ageSeconds": round(time.time() - self.fetched_at, 1),
            "error": self.error,
        }


def _f(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _minute_of_day(ts: Any) -> Optional[int]:
    try:
        dt = datetime.fromtimestamp(int(ts))
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    return dt.hour * 60 + dt.minute


class OpeningRangeEngine:
    """Cache + background-job manager for opening-range studies.

    A 5-year study is ~21 chunked API calls, so it can take ~30s. To keep the
    HTTP layer snappy, `study()` starts a background task (once) and returns the
    last known status; the UI polls until `status == "ready"`.
    """

    def __init__(self) -> None:
        self._cache: Dict[str, Tuple[float, OpeningRangeResult]] = {}
        self._jobs: Dict[str, asyncio.Task] = {}
        self._progress: Dict[str, Dict[str, Any]] = {}

    @staticmethod
    def _key(index_key: str, years: int) -> str:
        return f"{index_key.upper()}|{years}"

    # ---------- cache reads ----------
    def cached(self, index_key: str, years: int) -> Optional[OpeningRangeResult]:
        entry = self._cache.get(self._key(index_key, years))
        if not entry:
            return None
        ts, res = entry
        if time.time() - ts > CACHE_TTL_SECONDS:
            return None
        return res

    def is_running(self, index_key: str, years: int) -> bool:
        job = self._jobs.get(self._key(index_key, years))
        return job is not None and not job.done()

    def status(self, index_key: str, years: int) -> Dict[str, Any]:
        """Snapshot the study state for the polling UI."""
        key = self._key(index_key, years)
        if self.is_running(index_key, years):
            prog = self._progress.get(key) or {}
            return {"status": "computing", "progress": prog, "data": None}
        hit = self.cached(index_key, years)
        if hit is None:
            hit = self._latest_stale(index_key, years)
        if hit is not None:
            return {"status": "ready", "progress": None, "data": hit.to_dict()}
        return {"status": "idle", "progress": None, "data": None}

    def _latest_stale(self, index_key: str, years: int) -> Optional[OpeningRangeResult]:
        """A stale-but-present result (so we can show *something* while refreshing)."""
        entry = self._cache.get(self._key(index_key, years))
        return entry[1] if entry else None

    # ---------- job control ----------
    def start(self, inst: IndexInstrument, years: int, *, force: bool = False) -> None:
        """Kick off a background compute if one isn't already running (or force)."""
        years = max(1, min(int(years), MAX_YEARS))
        key = self._key(inst.key, years)
        if self.is_running(inst.key, years) and not force:
            return
        if force and self.is_running(inst.key, years):
            return  # already working; don't pile on
        self._progress[key] = {"done": 0, "total": max(1, years * 365 // MAX_DAYS_PER_CALL + 1)}
        task = asyncio.ensure_future(self._run(inst, years))
        self._jobs[key] = task

    async def _run(self, inst: IndexInstrument, years: int) -> None:
        key = self._key(inst.key, years)
        try:
            result = await self._compute(inst, years, progress_key=key)
            self._cache[key] = (time.time(), result)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("opening-range job failed for %s: %s", inst.key, exc)
            self._cache[key] = (
                time.time(),
                OpeningRangeResult(index_key=inst.key, years=years, error=str(exc)),
            )
        finally:
            self._progress.pop(key, None)

    async def study(
        self,
        inst: IndexInstrument,
        *,
        years: int = MAX_YEARS,
        force: bool = False,
    ) -> OpeningRangeResult:
        """Compute synchronously (used by warm-up / tests). Cached like the job path."""
        years = max(1, min(int(years), MAX_YEARS))
        if not force:
            hit = self.cached(inst.key, years)
            if hit is not None:
                return hit
        result = await self._compute(inst, years)
        self._cache[self._key(inst.key, years)] = (time.time(), result)
        return result

    async def _compute(
        self,
        inst: IndexInstrument,
        years: int,
        progress_key: Optional[str] = None,
    ) -> OpeningRangeResult:
        client = get_dhan_client()
        seg = "IDX_I"  # index value series (same for NSE + BSE indices)

        today = date.today()
        start = today - timedelta(days=years * 365 + 30)

        # Chunk into <=90-day intraday calls.
        chunks: List[Tuple[date, date]] = []
        cursor = start
        while cursor < today:
            end = min(cursor + timedelta(days=MAX_DAYS_PER_CALL), today)
            chunks.append((cursor, end))
            cursor = end + timedelta(days=1)

        days: List[DayRange] = []
        err: Optional[str] = None

        total = len(chunks)

        async def fetch_chunk(
            i: int, c_start: date, c_end: date
        ) -> Tuple[int, Optional[List[DayRange]], Optional[str]]:
            # Bounded-concurrency bulk fetch with 429 (DH-904) back-off retry.
            async with sem:
                for attempt in range(RATE_LIMIT_RETRIES):
                    try:
                        raw = await client.get_intraday(
                            security_id=str(inst.sec_id),
                            exchange_segment=seg,
                            instrument="INDEX",
                            interval="1",
                            from_dt=c_start.strftime("%Y-%m-%d 09:15:00"),
                            to_dt=c_end.strftime("%Y-%m-%d 15:30:00"),
                            throttle=False,  # bulk job manages its own pacing
                        )
                        return i, self._parse_chunk(raw), None
                    except DhanAPIError as exc:
                        if exc.error_code == "DH-904" and attempt < RATE_LIMIT_RETRIES - 1:
                            await asyncio.sleep(0.6 * (attempt + 1))
                            continue
                        logger.warning("intraday failed for %s: %s", inst.key, exc)
                        return i, None, exc.error_message
                    except Exception as exc:  # pragma: no cover - network best effort
                        return i, None, str(exc)
            return i, None, "rate limited"

        sem = asyncio.Semaphore(FETCH_CONCURRENCY)
        pending = [
            asyncio.ensure_future(fetch_chunk(i, a, b))
            for i, (a, b) in enumerate(chunks)
        ]
        done = 0
        for fut in asyncio.as_completed(pending):
            i, parsed, chunk_err = await fut
            done += 1
            if progress_key is not None:
                self._progress[progress_key] = {"done": done, "total": total}
            if chunk_err:
                err = chunk_err
            if parsed:
                days.extend(parsed)

        if progress_key is not None:
            self._progress[progress_key] = {"done": total, "total": total}

        if not days:
            return OpeningRangeResult(
                index_key=inst.key, years=years, error=err or "no data"
            )

        days.sort(key=lambda x: x.d)
        return self._summarize(inst.key, years, days, err)

    @staticmethod
    def _parse_chunk(raw: Any) -> List[DayRange]:
        """Group one intraday response by day and pull the 09:15-09:25 band."""
        data = raw.get("data") if isinstance(raw, dict) and "data" in raw else raw
        if not isinstance(data, dict):
            return []
        opens = data.get("open") or []
        highs = data.get("high") or []
        lows = data.get("low") or []
        closes = data.get("close") or []
        stamps = data.get("timestamp") or []
        n = min(len(opens), len(highs), len(lows), len(closes), len(stamps))
        if n == 0:
            return []

        # Bucket minute rows per calendar day.
        buckets: Dict[date, List[int]] = {}
        for i in range(n):
            ts = stamps[i]
            try:
                d = datetime.fromtimestamp(int(ts)).date()
            except (TypeError, ValueError, OverflowError, OSError):
                continue
            buckets.setdefault(d, []).append(i)

        out: List[DayRange] = []
        for d, idxs in buckets.items():
            win = [
                i
                for i in idxs
                if WINDOW_START_MIN <= (_minute_of_day(stamps[i]) or -1) <= WINDOW_END_MIN
            ]
            if len(win) < 2:
                continue  # not enough of the window captured
            win.sort(key=lambda i: stamps[i])
            o = _f(opens[win[0]])
            hi_w = [x for x in (_f(highs[i]) for i in win) if x is not None]
            lo_w = [x for x in (_f(lows[i]) for i in win) if x is not None]
            if o is None or not hi_w or not lo_w:
                continue
            day_hi = [x for x in (_f(highs[i]) for i in idxs) if x is not None]
            day_lo = [x for x in (_f(lows[i]) for i in idxs) if x is not None]
            if not day_hi or not day_lo:
                continue
            last_i = max(idxs, key=lambda i: stamps[i])
            day_close = _f(closes[last_i])
            if day_close is None:
                continue
            out.append(
                DayRange(
                    d=d,
                    open=o,
                    or_high=max(hi_w),
                    or_low=min(lo_w),
                    day_high=max(day_hi),
                    day_low=min(day_lo),
                    day_close=day_close,
                )
            )
        return out

    @staticmethod
    def _summarize(
        index_key: str,
        years: int,
        days: List[DayRange],
        err: Optional[str],
    ) -> OpeningRangeResult:
        res = OpeningRangeResult(index_key=index_key, years=years, error=err)
        res.sample_days = len(days)

        ranges_pct = [x.range_pct for x in days if x.range_pct is not None]
        ranges_pts = [x.range_pts for x in days]
        day_ranges = [x.day_range_pct for x in days if x.day_range_pct is not None]

        if ranges_pct:
            res.avg_range_pct = round(sum(ranges_pct) / len(ranges_pct), 3)
            res.median_range_pct = round(statistics.median(ranges_pct), 3)
            res.min_range_pct = round(min(ranges_pct), 3)
            res.max_range_pct = round(max(ranges_pct), 3)
            srt = sorted(ranges_pct)
            p90_idx = min(len(srt) - 1, int(round(0.9 * (len(srt) - 1))))
            res.p90_range_pct = round(srt[p90_idx], 3)
        if ranges_pts:
            res.avg_range_points = round(sum(ranges_pts) / len(ranges_pts), 2)
        if day_ranges:
            res.avg_day_range_pct = round(sum(day_ranges) / len(day_ranges), 3)
            if res.avg_range_pct and res.avg_day_range_pct:
                res.opening_share_of_day_pct = round(
                    res.avg_range_pct / res.avg_day_range_pct * 100, 1
                )

        # Post-window behaviour.
        n = len(days)
        if n:
            up = sum(1 for x in days if x.day_high > x.or_high)
            down = sum(1 for x in days if x.day_low < x.or_low)
            close_above = sum(1 for x in days if x.day_close > x.or_high)
            close_below = sum(1 for x in days if x.day_close < x.or_low)
            inside = n - close_above - close_below
            res.break_up_rate = round(up / n * 100, 1)
            res.break_down_rate = round(down / n * 100, 1)
            res.close_above_rate = round(close_above / n * 100, 1)
            res.close_below_rate = round(close_below / n * 100, 1)
            res.close_inside_rate = round(inside / n * 100, 1)

        # Distribution buckets (by opening-range %).
        edges = [(0.0, 0.2), (0.2, 0.35), (0.35, 0.5), (0.5, 0.75), (0.75, 1.0), (1.0, 1e9)]
        labels = [
            "< 0.20%",
            "0.20-0.35%",
            "0.35-0.50%",
            "0.50-0.75%",
            "0.75-1.0%",
            "> 1.0%",
        ]
        buckets = []
        for (lo, hi), label in zip(edges, labels):
            c = sum(1 for p in ranges_pct if lo <= p < hi)
            buckets.append(
                {
                    "label": label,
                    "count": c,
                    "pct": round(c / len(ranges_pct) * 100, 1) if ranges_pct else 0.0,
                }
            )
        res.range_buckets = buckets

        # Most recent N sessions, newest first.
        recent = sorted(days, key=lambda x: x.d, reverse=True)[:RECENT_SAMPLES]
        res.recent = [
            {
                "date": x.d.isoformat(),
                "open": x.open,
                "orHigh": x.or_high,
                "orLow": x.or_low,
                "rangePoints": x.range_pts,
                "rangePct": x.range_pct,
                "dayHigh": x.day_high,
                "dayLow": x.day_low,
                "dayClose": x.day_close,
                "brokeUp": x.day_high > x.or_high,
                "brokeDown": x.day_low < x.or_low,
            }
            for x in recent
        ]
        return res


opening_range_engine = OpeningRangeEngine()
