"""Algo 2 end-of-day research cache (permanent, never pruned).

Once per trading day, after the close (>= 15:31 IST), this downloads that day's
1-minute candles from the Master Dhan data API into
``history/YYYY-MM-DD/candles/`` and writes ``day_summary.json``:

  NIFTY_spot, SENSEX_spot      (IDX_I / INDEX)
  NIFTY_fut, SENSEX_fut        (nearest FUTIDX, with open interest)
  INDIAVIX                     (IDX_I / INDEX)
  RELIANCE, HDFCBANK, ...      (NSE_EQ / EQUITY heavyweights)

About 11 calls, spaced ``CALL_GAP_SECONDS`` apart, run as a background task so
the signal loop is never blocked. It never runs during market hours (the
Master data quota is shared with live trading). A rate-limit (429 / DH-904)
stops the run and it retries later; a day missed because the server was off
is caught up on the next start (only days that have a history folder).
``candles/done.json`` marks a finished day so it is never downloaded twice.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections import Counter
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Tuple

from ..algo import session_for
from ..algo_store import AlgoStore
from ..dhan_client import DhanAPIError, get_dhan_client as get_data_client
from ..instruments import get_index
from .market_data import HEAVYWEIGHT_IDS, INDIA_VIX_ID, Algo2MarketData, parse_candles

logger = logging.getLogger("dhan.algo2.cache")

MARKET_OPEN_MIN = 9 * 60 + 15
CACHE_START_MIN = 15 * 60 + 31
CALL_GAP_SECONDS = 3.0
CATCHUP_DAYS = 5
RETRY_SECONDS = 600.0
MAX_ATTEMPTS = 3
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

Instrument = Tuple[str, str, str, str, bool]   # name, securityId, segment, instrument, oi


def _read_jsonl(path: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
    except OSError:
        return []
    return rows


def _read_json(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: str, data: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, default=str, separators=(",", ":"))
    os.replace(tmp, path)


class Algo2DailyCache:
    def __init__(self, history_dir: str, data: Algo2MarketData,
                 stores: Callable[[], Dict[str, AlgoStore]],
                 sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep) -> None:
        self.dir = history_dir
        self.data = data
        self.stores = stores
        self._sleep = sleep
        self._task: Optional[asyncio.Task] = None
        self._next_try = 0.0
        self._attempts: Dict[str, int] = {}
        self.last_day: Optional[str] = None
        self.error: Optional[str] = None

    # ------------------------------------------------------------ scheduling
    def is_done(self, day: str) -> bool:
        return os.path.exists(os.path.join(self.dir, day, "candles", "done.json"))

    def pending_days(self, now: datetime) -> List[str]:
        today = now.date().isoformat()
        try:
            past = sorted(n for n in os.listdir(self.dir) if _DAY_RE.match(n) and n < today)
        except OSError:
            past = []
        days = [d for d in past[-CATCHUP_DAYS:] if not self.is_done(d)]
        minute = now.hour * 60 + now.minute
        if session_for(now).trading_day and minute >= CACHE_START_MIN and not self.is_done(today):
            days.append(today)
        return days

    def due(self, now: datetime) -> bool:
        minute = now.hour * 60 + now.minute
        if session_for(now).trading_day and MARKET_OPEN_MIN <= minute < CACHE_START_MIN:
            return False   # keep the Master data quota free for live trading
        return time.time() >= self._next_try and bool(self.pending_days(now))

    def schedule(self, now: datetime, indices: Iterable[str]) -> Optional[asyncio.Task]:
        """Start the cache run in the background when due (cheap no-op otherwise)."""
        if self._task is not None and not self._task.done():
            return None
        if not self.due(now):
            return None
        self._task = asyncio.create_task(self.run(now, list(indices)), name="algo2-daily-cache")
        return self._task

    async def run(self, now: datetime, indices: List[str]) -> None:
        for day in self.pending_days(now):
            try:
                ok = await self.cache_day(day, indices, now)
            except Exception as exc:  # noqa: BLE001 - research cache must never hurt trading
                logger.exception("Algo 2 daily cache failed for %s", day)
                self.error, ok = f"{day}: {exc}", False
            if not ok:
                self._next_try = time.time() + RETRY_SECONDS
                return

    # ---------------------------------------------------------------- work
    async def _instruments(self, indices: List[str], now: datetime) -> List[Instrument]:
        out: List[Instrument] = []
        for key in indices:
            inst = get_index(key)
            if inst is not None:
                out.append((f"{key}_spot", str(inst.sec_id), "IDX_I", "INDEX", False))
        await self.data.ensure_futures(now.date())
        for key in indices:
            fut = self.data.futures.get(key)
            if fut:
                out.append((f"{key}_fut", str(fut["securityId"]), fut["segment"], "FUTIDX", True))
        out.append(("INDIAVIX", str(INDIA_VIX_ID), "IDX_I", "INDEX", False))
        out.extend((sym, str(sid), "NSE_EQ", "EQUITY", False) for sym, sid in HEAVYWEIGHT_IDS.items())
        return out

    @staticmethod
    def _candles(raw: Any) -> List[Dict[str, float]]:
        candles = parse_candles(raw)
        data = raw.get("data") if isinstance(raw, dict) and "data" in raw else raw
        oi = data.get("open_interest") if isinstance(data, dict) else None
        stamps = data.get("timestamp") if isinstance(data, dict) else None
        if oi and stamps:
            by_t = {}
            for t, v in zip(stamps, oi):
                try:
                    by_t[float(t)] = float(v)
                except (TypeError, ValueError):
                    continue
            for c in candles:
                if c["t"] in by_t:
                    c["oi"] = by_t[c["t"]]
        return candles

    async def cache_day(self, day: str, indices: List[str], now: datetime) -> bool:
        """Download one day's 1-min candles. False = rate-limited / failed, retry later."""
        folder = os.path.join(self.dir, day, "candles")
        os.makedirs(folder, exist_ok=True)
        client = get_data_client()
        errors: Dict[str, str] = {}
        for name, sec_id, segment, instrument, with_oi in await self._instruments(indices, now):
            path = os.path.join(folder, f"{name}.json")
            if os.path.exists(path):
                continue   # resumed run: already saved
            try:
                raw = await client.get_intraday(sec_id, segment, instrument, "1",
                                                f"{day} 09:15:00", f"{day} 15:30:00", oi=with_oi)
            except DhanAPIError as exc:
                await self._sleep(CALL_GAP_SECONDS)
                if exc.status_code == 429 or "904" in str(exc.error_code or ""):
                    self.error = f"{day}: rate limited ({exc.error_message}), retry in {int(RETRY_SECONDS)}s"
                    logger.warning("Algo 2 daily cache %s", self.error)
                    return False
                errors[name] = exc.error_message
                continue
            await self._sleep(CALL_GAP_SECONDS)
            candles = self._candles(raw)
            _write_json(path, {"day": day, "name": name, "securityId": sec_id, "segment": segment,
                               "instrument": instrument, "interval": "1", "candles": candles})
        if errors:
            self._attempts[day] = self._attempts.get(day, 0) + 1
            if self._attempts[day] < MAX_ATTEMPTS:
                self.error = f"{day}: {len(errors)} download error(s), will retry"
                return False
        counts = {n[:-len(".json")]: len(_read_json(os.path.join(folder, n)).get("candles") or [])
                  for n in sorted(os.listdir(folder)) if n.endswith(".json") and n != "done.json"}
        summary = self.day_summary(day)
        summary["candles"] = counts
        summary["candleErrors"] = errors
        _write_json(os.path.join(self.dir, day, "day_summary.json"), summary)
        _write_json(os.path.join(folder, "done.json"),
                    {"day": day, "at": datetime.now().isoformat(timespec="seconds"),
                     "saved": counts, "errors": errors})
        self.last_day, self.error = day, None
        logger.info("Algo 2 daily cache saved %s (%d files, %d errors)", day, len(counts), len(errors))
        return True

    def day_summary(self, day: str) -> Dict[str, Any]:
        folder = os.path.join(self.dir, day)
        rows = _read_jsonl(os.path.join(folder, "breakouts.jsonl"))
        breakouts = [r for r in rows if r.get("type") == "breakout"]
        outcomes = [r for r in rows if r.get("type") == "outcome"]
        scores: Counter = Counter()
        blockers: Counter = Counter()
        for r in breakouts:
            side_score = ((r.get("score") or {}).get(r.get("side")) or {})
            if side_score.get("passed") is not None:
                scores[f"{side_score['passed']}/{side_score.get('total')}"] += 1
            for b in r.get("blockers") or []:
                blockers["score_below_min" if str(b).startswith("score ") else str(b)] += 1
        trades: Dict[str, Any] = {}
        for account, store in self.stores().items():
            day_trades = [t for t in store.read_trades(limit=5000) if str(t.get("date", "")) == day]
            trades[account] = {
                "count": len(day_trades),
                "premiumPnl": round(sum(float(t.get("premiumPnl") or 0) for t in day_trades), 2),
                "exitReasons": dict(Counter(str(t.get("exitReason")) for t in day_trades)),
            }
        return {
            "day": day,
            "breakouts": len(breakouts),
            "taken": sum(1 for r in breakouts if r.get("taken")),
            "skipped": sum(1 for r in breakouts if not r.get("taken")),
            "bySide": dict(Counter(f"{r.get('index')} {r.get('side')}" for r in breakouts)),
            "scoreDistribution": dict(scores),
            "blockers": dict(blockers.most_common()),
            "outcomes": dict(Counter(str(r.get("firstHit")) for r in outcomes)),
            "skippedWouldHit2R": sum(1 for r in outcomes if not r.get("taken") and r.get("firstHit") == "target_2R"),
            "reports": len(_read_jsonl(os.path.join(folder, "reports.jsonl"))),
            "metricsRows": len(_read_jsonl(os.path.join(folder, "metrics.jsonl"))),
            "chainSnapshots": len(_read_jsonl(os.path.join(folder, "chain.jsonl"))),
            "trades": trades,
        }

    async def cancel(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    def status(self) -> Dict[str, Any]:
        return {"lastDay": self.last_day, "error": self.error,
                "running": self._task is not None and not self._task.done()}
