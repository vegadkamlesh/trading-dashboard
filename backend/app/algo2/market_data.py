"""Algo 2 market data (Master Dhan data API only, gently throttled).

Budget while Algo 2 is ON (or holding a position) during market hours:
  * 1 batched ``/marketfeed/ohlc`` call every ``QUOTE_SECONDS`` (index, VIX,
    heavyweights, current-month futures) - Dhan allows 1/sec.
  * 4 ``/charts/intraday`` 5-minute calls per 5 minutes (spot + futures for
    NIFTY and SENSEX) through the shared heavy-chart gate.
  * Option chains are NOT fetched here: the existing chain engine already
    refreshes each index every ~3.5s; Algo 2 only reads its snapshots.
Nothing is fetched while Algo 2 is OFF and flat.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import os
import time
from collections import deque
from datetime import date, datetime, timedelta
from typing import Any, Deque, Dict, List, Optional, Tuple

import httpx

from ..dhan_client import DhanAPIError, get_dhan_client as get_data_client
from ..feed import feed
from ..instruments import get_index
from ..optionchain import engine as chain_engine
from . import calculations as calc

logger = logging.getLogger("dhan.algo2.data")

SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
QUOTE_SECONDS = 5.0
CHAIN_SAMPLE_SECONDS = 15.0
VELOCITY_WINDOW = 300.0      # 5 minutes
HISTORY_SECONDS = 900.0
MAX_SPOT_AGE = 5.0

# Verified from the Dhan scrip master (NSE_EQ security ids).
HEAVYWEIGHT_IDS: Dict[str, int] = {
    "RELIANCE": 2885, "HDFCBANK": 1333, "ICICIBANK": 4963,
    "SBIN": 3045, "TCS": 11536, "BHARTIARTL": 10604,
}
INDIA_VIX_ID = 21
FUT_EXCHANGE = {"NIFTY": ("NSE", "NSE_FNO"), "SENSEX": ("BSE", "BSE_FNO")}


def _ist_now() -> datetime:
    from ..algo import now_ist
    return now_ist()


class Series:
    """Rolling (epoch, value) samples for 'value N seconds ago' lookups."""

    def __init__(self, keep: float = HISTORY_SECONDS) -> None:
        self.keep = keep
        self.items: Deque[Tuple[float, float]] = deque()

    def add(self, value: Optional[float], at: Optional[float] = None) -> None:
        if value is None:
            return
        at = time.time() if at is None else at
        self.items.append((at, float(value)))
        while self.items and self.items[0][0] < at - self.keep:
            self.items.popleft()

    def latest(self) -> Optional[float]:
        return self.items[-1][1] if self.items else None

    def ago(self, seconds: float, now: Optional[float] = None, tolerance: float = 90.0) -> Optional[float]:
        """Value at least ``seconds`` old (newest such sample), None if history is too short."""
        now = time.time() if now is None else now
        target = now - seconds
        found = None
        for at, value in self.items:
            if at <= target:
                found = (at, value)
            else:
                break
        if found is None or target - found[0] > tolerance:
            return None
        return found[1]

    def change_pct(self, seconds: float) -> Optional[float]:
        before, now = self.ago(seconds), self.latest()
        if not before or now is None:
            return None
        return (now - before) / before * 100.0


def parse_candles(raw: Any) -> List[Dict[str, float]]:
    data = raw.get("data") if isinstance(raw, dict) and "data" in raw else raw
    if not isinstance(data, dict):
        return []
    cols = [data.get(k) or [] for k in ("timestamp", "open", "high", "low", "close")]
    volume = data.get("volume") or []
    out: List[Dict[str, float]] = []
    for i in range(min(len(c) for c in cols)):
        try:
            candle = {"t": float(cols[0][i]), "open": float(cols[1][i]), "high": float(cols[2][i]),
                      "low": float(cols[3][i]), "close": float(cols[4][i]),
                      "volume": float(volume[i]) if i < len(volume) and volume[i] is not None else 0.0}
        except (TypeError, ValueError):
            continue
        if candle["high"] > 0 and candle["low"] > 0:
            out.append(candle)
    out.sort(key=lambda c: c["t"])
    return out


def resolve_futures_from_csv(text: str, today: date) -> Dict[str, Dict[str, Any]]:
    """Nearest non-expired FUTIDX contract per index from the scrip-master CSV."""
    best: Dict[str, Dict[str, Any]] = {}
    for row in csv.DictReader(io.StringIO(text)):
        if row.get("SEM_INSTRUMENT_NAME") != "FUTIDX":
            continue
        symbol = str(row.get("SEM_TRADING_SYMBOL") or "")
        root = symbol.split("-")[0]
        if root not in FUT_EXCHANGE or row.get("SEM_EXM_EXCH_ID") != FUT_EXCHANGE[root][0]:
            continue
        try:
            expiry = datetime.strptime(str(row["SEM_EXPIRY_DATE"])[:10], "%Y-%m-%d").date()
            sec_id = int(float(row["SEM_SMST_SECURITY_ID"]))
        except (KeyError, ValueError):
            continue
        if expiry < today:
            continue
        if root not in best or expiry.isoformat() < best[root]["expiry"]:
            best[root] = {"securityId": sec_id, "segment": FUT_EXCHANGE[root][1],
                          "symbol": symbol, "expiry": expiry.isoformat()}
    return best


class Algo2MarketData:
    def __init__(self, directory: str) -> None:
        self.dir = directory
        self.futures: Dict[str, Dict[str, Any]] = {}
        self._futures_day: Optional[str] = None
        self.futures_error: Optional[str] = None
        self.quotes: Dict[Tuple[str, int], Dict[str, float]] = {}
        self.quote_series: Dict[Tuple[str, int], Series] = {}
        self.quote_at: Optional[float] = None
        self.quote_error: Optional[str] = None
        self.spot_candles: Dict[str, List[Dict[str, float]]] = {}
        self.fut_candles: Dict[str, List[Dict[str, float]]] = {}
        self.candles_for: Dict[str, float] = {}
        self.candle_error: Optional[str] = None
        self.oi_series: Dict[str, Dict[str, Series]] = {}
        self.delta_series: Dict[str, Series] = {}
        self.basis_series: Dict[str, Series] = {}
        self._last_chain_sample: Dict[str, float] = {}
        self._last_quote_poll = 0.0
        self._next_candle_try: Dict[str, float] = {}
        self.calls = {"quote": 0, "candles": 0, "scripMaster": 0}

    # ---------------------------------------------------------- futures
    async def ensure_futures(self, today: date) -> None:
        if self._futures_day == today.isoformat() and self.futures:
            return
        cache = os.path.join(self.dir, "futures.json")
        try:
            with open(cache, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if data.get("day") == today.isoformat() and data.get("futures"):
                self.futures, self._futures_day = data["futures"], data["day"]
                return
        except (OSError, ValueError):
            pass
        try:
            self.calls["scripMaster"] += 1
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.get(SCRIP_MASTER_URL)
                resp.raise_for_status()
            futures = await asyncio.to_thread(resolve_futures_from_csv, resp.text, today)
        except (httpx.HTTPError, ValueError) as exc:
            self.futures_error = f"Futures contract lookup failed: {exc}"
            logger.warning(self.futures_error)
            return
        if not futures:
            self.futures_error = "No current FUTIDX contract found in the Dhan scrip master"
            return
        self.futures, self._futures_day, self.futures_error = futures, today.isoformat(), None
        os.makedirs(self.dir, exist_ok=True)
        with open(cache, "w", encoding="utf-8") as fh:
            json.dump({"day": self._futures_day, "futures": futures}, fh, indent=2)

    # ----------------------------------------------------------- quotes
    def _instruments(self, indices: List[str]) -> Dict[str, List[int]]:
        out: Dict[str, List[int]] = {"IDX_I": [INDIA_VIX_ID], "NSE_EQ": list(HEAVYWEIGHT_IDS.values())}
        for key in indices:
            inst = get_index(key)
            if inst:
                out["IDX_I"].append(inst.sec_id)
            fut = self.futures.get(key)
            if fut:
                out.setdefault(fut["segment"], []).append(int(fut["securityId"]))
        return out

    async def poll_quotes(self, indices: List[str], force: bool = False) -> None:
        if not force and time.monotonic() - self._last_quote_poll < QUOTE_SECONDS:
            return
        self._last_quote_poll = time.monotonic()
        try:
            self.calls["quote"] += 1
            raw = await get_data_client().get_ohlc(self._instruments(indices))
        except DhanAPIError as exc:
            self.quote_error = exc.error_message
            return
        data = raw.get("data") if isinstance(raw, dict) else None
        if not isinstance(data, dict):
            self.quote_error = "Quote response had no data"
            return
        now = time.time()
        for segment, rows in data.items():
            if not isinstance(rows, dict):
                continue
            for sid, row in rows.items():
                try:
                    ltp = float(row.get("last_price"))
                except (TypeError, ValueError, AttributeError):
                    continue
                if ltp <= 0:
                    continue
                ohlc = row.get("ohlc") or {}
                key = (segment, int(sid))
                self.quotes[key] = {"ltp": ltp, "prevClose": float(ohlc.get("close") or 0),
                                    "open": float(ohlc.get("open") or 0), "at": now}
                self.quote_series.setdefault(key, Series()).add(ltp, now)
        self.quote_at, self.quote_error = now, None

    def quote(self, segment: str, sid: int) -> Optional[Dict[str, float]]:
        return self.quotes.get((segment, int(sid)))

    def day_change_pct(self, segment: str, sid: int) -> Optional[float]:
        q = self.quote(segment, sid)
        if not q or not q.get("prevClose"):
            return None
        return (q["ltp"] - q["prevClose"]) / q["prevClose"] * 100.0

    def change_pct(self, segment: str, sid: int, seconds: float = VELOCITY_WINDOW) -> Optional[float]:
        series = self.quote_series.get((segment, int(sid)))
        return series.change_pct(seconds) if series else None

    def spot(self, index_key: str) -> Optional[float]:
        inst = get_index(index_key)
        if inst is None:
            return None
        age = feed.age(inst.sec_id, "IDX_I")
        price = feed.price(inst.sec_id, "IDX_I")
        if price and age is not None and age <= MAX_SPOT_AGE:
            return float(price)
        q = self.quote("IDX_I", inst.sec_id)
        if q and time.time() - q["at"] <= QUOTE_SECONDS * 2 + 1:
            return q["ltp"]
        return None

    def futures_price(self, index_key: str) -> Optional[float]:
        fut = self.futures.get(index_key)
        if not fut:
            return None
        q = self.quote(fut["segment"], fut["securityId"])
        if q and time.time() - q["at"] <= QUOTE_SECONDS * 2 + 1:
            return q["ltp"]
        return None

    # ---------------------------------------------------------- candles
    async def refresh_candles(self, index_key: str, now: datetime) -> None:
        """Fetch today's 5-minute spot + futures candles once per closed 5-minute bar."""
        bar_end = now.replace(second=0, microsecond=0)
        bar_end -= timedelta(minutes=bar_end.minute % 5)
        boundary = bar_end.timestamp()
        if self.candles_for.get(index_key) == boundary:
            return
        if time.time() < self._next_candle_try.get(index_key, 0.0) or (now - bar_end).total_seconds() < 3:
            return
        inst = get_index(index_key)
        if inst is None:
            return
        frm = f"{now.date().isoformat()} 09:15:00"
        to = (bar_end + timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
        client = get_data_client()
        try:
            self.calls["candles"] += 1
            spot_raw = await client.get_intraday(str(inst.sec_id), "IDX_I", "INDEX", "5", frm, to)
            fut = self.futures.get(index_key)
            fut_raw = None
            if fut:
                self.calls["candles"] += 1
                fut_raw = await client.get_intraday(str(fut["securityId"]), fut["segment"], "FUTIDX", "5", frm, to)
        except DhanAPIError as exc:
            self.candle_error = f"{index_key}: {exc.error_message}"
            self._next_candle_try[index_key] = time.time() + (60 if exc.status_code == 429 or "904" in exc.error_code else 15)
            return
        spot = [c for c in parse_candles(spot_raw) if c["t"] < boundary]
        futs = [c for c in parse_candles(fut_raw) if c["t"] < boundary] if fut_raw is not None else []
        if not spot or spot[-1]["t"] < boundary - 300:
            # Dhan has not published the just-closed bar yet; retry shortly.
            self._next_candle_try[index_key] = time.time() + 5
            self.spot_candles[index_key] = spot or self.spot_candles.get(index_key, [])
            self.fut_candles[index_key] = futs or self.fut_candles.get(index_key, [])
            return
        self.spot_candles[index_key] = spot
        self.fut_candles[index_key] = futs
        self.candles_for[index_key] = boundary
        self.candle_error = None

    def last_closed_candle(self, index_key: str) -> Optional[Dict[str, float]]:
        candles = self.spot_candles.get(index_key) or []
        return candles[-1] if candles else None

    # ------------------------------------------------------------ chain
    def chain_rows(self, index_key: str) -> List[Dict[str, Any]]:
        snap = chain_engine.snapshot(index_key) or {}
        return list(snap.get("rows") or [])

    def chain_expiry(self, index_key: str) -> Optional[str]:
        snap = chain_engine.snapshot(index_key) or {}
        return snap.get("expiry")

    def sample_chain(self, index_key: str, spot: Optional[float], strikes_each_side: int) -> None:
        if time.time() - self._last_chain_sample.get(index_key, 0.0) < CHAIN_SAMPLE_SECONDS:
            return
        rows = self.chain_rows(index_key)
        if not rows or not spot:
            return
        near = calc.near_rows(rows, spot, strikes_each_side)
        ce, pe = calc.oi_totals(near)
        if ce <= 0 and pe <= 0:
            return
        now = time.time()
        series = self.oi_series.setdefault(index_key, {"ce": Series(), "pe": Series()})
        series["ce"].add(ce, now)
        series["pe"].add(pe, now)
        self.delta_series.setdefault(index_key, Series()).add(calc.compute_net_delta(near), now)
        self.basis_series.setdefault(index_key, Series()).add(
            calc.compute_basis(spot, self.futures_price(index_key)), now)
        self._last_chain_sample[index_key] = now

    def oi_velocity(self, index_key: str) -> Dict[str, Optional[float]]:
        series = self.oi_series.get(index_key)
        if not series:
            return {"ce": None, "pe": None}
        current = {s: series[s].latest() or 0.0 for s in ("ce", "pe")}
        previous = {s: series[s].ago(VELOCITY_WINDOW) or 0.0 for s in ("ce", "pe")}
        return calc.compute_oi_velocity(current, previous)

    def status(self) -> Dict[str, Any]:
        return {
            "futures": self.futures,
            "futuresError": self.futures_error,
            "quoteAgeSeconds": round(time.time() - self.quote_at, 1) if self.quote_at else None,
            "quoteError": self.quote_error,
            "candleError": self.candle_error,
            "candles": {k: len(v) for k, v in self.spot_candles.items()},
            "apiCalls": dict(self.calls),
        }
