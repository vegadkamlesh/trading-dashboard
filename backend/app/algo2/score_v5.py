"""Algo 2 v5 rule set: NIFTY-only, 24-feature score >= 17, VIX gate, 15-min candle SL.

Live port of the 6-month backtest (May-Nov 2026) feature study. Every feature is
evaluated at the FIRST touch of a 5-minute breakout (per candle and side), using
data up to that moment only. ``pos`` features earn a point when True, ``neg``
features earn a point when False. Data that is missing live earns NO point
(conservative), so an outage can only block entries, never add them.

Gates (all must pass): inside 09:45-15:00, India VIX not spiking against the
trade (3%), and no entry on expiry day between 10:45 and 11:45.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import os
import statistics
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

from ..dhan_client import DhanAPIError, get_dhan_client as get_data_client

logger = logging.getLogger("dhan.algo2.v5")

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
NIFTY_ID, VIX_ID, BANKNIFTY_ID = 13, 21, 25

POS: Tuple[str, ...] = ("oi_unwinding", "x_flow_delta", "f08_vix_low", "f16_toward_maxpain")
NEG: Tuple[str, ...] = (
    "vwap_band", "basis", "x_above_vwap", "x_day_trend", "x_orb_break", "f01_orb15_break", "f01_orb30_break",
    "f02_day_dir", "f03_vwap_side", "f03_vwap_pullback", "f06_prev_break", "f09_dte_le1", "f09_exp_pm",
    "f10_wall_room", "f17_client_contra", "f19_bank_15m", "f22_es_overnight", "f22_spx_prev", "f22_usvix_down",
    "f22_inr_strong",
)
FEATURES: Tuple[str, ...] = POS + NEG
GATES: Tuple[str, ...] = ("time_window", "vix", "expiry_block", "expiry_eve", "fut_book", "fut_depth200")

# v7 (2026-11-22, 2-year per-condition study): only the 7 conditions with a steady edge; same feature
# inputs as v5 but VIX is inverted (a point when the VIX open is NOT below its 20-day median).
V7_POS: Tuple[str, ...] = ("oi_unwinding", "x_flow_delta")
V7_NEG: Tuple[str, ...] = ("f08_vix_low", "basis", "f09_exp_pm", "f10_wall_room", "f19_bank_15m")
# v8 (2026-11-23, 2-year Greek study of 16 IV/gamma/theta/vega metrics): v7 + one vega point, the only
# metric that beat v7 in every fold, beat 20 random-point placebos and held out of sample; need 6/8.
IV_WINDOW_SECONDS = 900.0
IV_RISE_MIN = -0.1   # ATM IV points: "not falling" over IV_WINDOW_SECONDS
V8_POS: Tuple[str, ...] = V7_POS + ("x_iv_rising",)
MODELS: Dict[str, Tuple[Tuple[str, ...], Tuple[str, ...]]] = {
    "v5": (POS, NEG), "v7": (V7_POS, V7_NEG), "v8": (V8_POS, V7_NEG)}

LABELS: Dict[str, str] = {
    "time_window": "Inside a trade window",
    "vix": "India VIX stable (< 3% intraday move against)",
    "expiry_block": "Not expiry day 10:45-11:45",
    "expiry_eve": "Not the trading day before expiry",
    "fut_book": "Futures total buy qty vs sell qty with the trade (TBS)",
    "fut_depth200": "Futures 200-level book imbalance agrees with the trade",
    "oi_unwinding": "Same-side OI unwinding (5 min)",
    "x_flow_delta": "OI flow x delta with the trade (5 min)",
    "x_iv_rising": "ATM IV not falling (15 min change > -0.1)",
    "f08_vix_low": "VIX open below its 20-day median",
    "f16_toward_maxpain": "Max pain lies in the trade direction",
    "vwap_band": "NOT: futures beyond VWAP +/-1 sd band",
    "basis": "NOT: futures basis confirms",
    "x_above_vwap": "NOT: futures on the trade side of 5-min VWAP",
    "x_day_trend": "NOT: entry beyond today's open",
    "x_orb_break": "NOT: entry beyond 09:15-09:45 range",
    "f01_orb15_break": "NOT: entry beyond 09:15-09:30 range",
    "f01_orb30_break": "NOT: entry beyond 09:15-09:45 range (ORB30)",
    "f02_day_dir": "NOT: spot beyond previous close",
    "f03_vwap_side": "NOT: futures on the trade side of 1-min VWAP",
    "f03_vwap_pullback": "NOT: VWAP-side with a 30-min VWAP pullback",
    "f06_prev_break": "NOT: entry beyond previous day high/low",
    "f09_dte_le1": "NOT: expiry today or tomorrow",
    "f09_exp_pm": "NOT: expiry day after 13:00",
    "f10_wall_room": "NOT: OI wall >= 2x risk away (or no wall)",
    "f17_client_contra": "NOT: clients cut index futures the trade way",
    "f19_bank_15m": "NOT: BANKNIFTY moved with the trade (15 min)",
    "f22_es_overnight": "NOT: S&P futures overnight with the trade",
    "f22_spx_prev": "NOT: S&P 500 previous day with the trade",
    "f22_usvix_down": "NOT: US VIX moved for the trade",
    "f22_inr_strong": "NOT: INR moved for the trade",
}

CONTEXT_START_MIN = 9 * 60 + 16      # after 09:15 IST: global cues measured up to the open
CONTEXT_RETRY_SECONDS = 120.0
NSE_ARCHIVE = "https://nsearchives.nseindia.com/content/nsccl/fao_participant_oi_{d:%d%m%Y}.csv"
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                     "Chrome/124.0 Safari/537.36", "Accept": "*/*", "Referer": "https://www.nseindia.com/"}


V7_LABELS: Dict[str, str] = {**{k: LABELS[k] for k in V8_POS + V7_NEG},
                             "f08_vix_low": "NOT: VIX open below its 20-day median"}


def model_features(model: str = "v5") -> Tuple[str, ...]:
    pos, neg = MODELS[model]
    return pos + neg


def model_label(key: str, model: str = "v5") -> str:
    return (V7_LABELS if model in ("v7", "v8") else LABELS).get(key) or LABELS[key]


def points(features: Dict[str, Optional[bool]], model: str = "v5") -> int:
    """Score: pos features True + neg features False. Missing (None) earns nothing."""
    pos, neg = MODELS[model]
    return sum(features.get(k) is True for k in pos) + sum(features.get(k) is False for k in neg)


def earned(key: str, value: Optional[bool], model: str = "v5") -> bool:
    return value is True if key in MODELS[model][0] else value is False


def ist_date(ts: float) -> date:
    return datetime.fromtimestamp(ts, IST).date()


# --------------------------------------------------------------------------- candles
def session_start(now: datetime) -> float:
    return now.astimezone(IST).replace(hour=9, minute=15, second=0, microsecond=0).timestamp()


def last_closed_candle(candles5: List[Dict[str, float]], now: datetime, minutes: int) -> Optional[Dict[str, float]]:
    """Last fully closed N-minute candle (aligned at 09:15) built from closed 5-minute candles.

    None while the newest closed bucket's 5-minute parts are not all published yet.
    """
    if not candles5:
        return None
    if minutes == 5:
        return candles5[-1]
    start, size, now_ts = session_start(now), minutes * 60, now.timestamp()
    buckets: Dict[float, List[Dict[str, float]]] = {}
    for c in candles5:
        if c["t"] < start:
            continue
        b = c["t"] - (c["t"] - start) % size
        buckets.setdefault(b, []).append(c)
    closed = sorted(b for b in buckets if b + size <= now_ts)
    if not closed:
        return None
    b = closed[-1]
    parts = sorted(buckets[b], key=lambda c: c["t"])
    if len(parts) < minutes // 5:
        return None
    return {"t": b, "open": parts[0]["open"], "high": max(c["high"] for c in parts),
            "low": min(c["low"] for c in parts), "close": parts[-1]["close"], "minutes": minutes}


def range_between(candles5: List[Dict[str, float]], now: datetime, start_min: int, end_min: int
                  ) -> Optional[Tuple[float, float]]:
    """High/low of the 5-min spot candles from start_min to end_min (IST minutes); None if incomplete."""
    day = session_start(now) - (9 * 60 + 15) * 60
    lo_t, hi_t = day + start_min * 60, day + end_min * 60
    rows = [c for c in candles5 if lo_t <= c["t"] < hi_t]
    if len(rows) < (end_min - start_min) // 5:
        return None
    return max(c["high"] for c in rows), min(c["low"] for c in rows)


def vwap_path(fut1m: List[Dict[str, float]]) -> List[Tuple[float, float, float]]:
    """(t, futures close, cumulative VWAP) per closed 1-minute futures candle."""
    num = den = 0.0
    out = []
    for c in fut1m:
        v = c.get("volume") or 1.0
        num += c["close"] * v
        den += v
        out.append((c["t"], c["close"], num / den))
    return out


# --------------------------------------------------------------------------- chain
def max_pain(rows: List[Dict[str, Any]]) -> Optional[float]:
    """Max pain on the previous day's closing OI (``prevOi``) of the current expiry."""
    ce: Dict[float, float] = {}
    pe: Dict[float, float] = {}
    for r in rows:
        k = float(r["strike"])
        for leg, book in (("ce", ce), ("pe", pe)):
            oi = (r.get(leg) or {}).get("prevOi")
            if oi is not None:
                book[k] = float(oi or 0)
    strikes = sorted(set(ce) | set(pe))
    if len(strikes) < 5 or not any(ce.values()) and not any(pe.values()):
        return None
    pain = {s: sum(ce.get(k, 0) * max(s - k, 0) + pe.get(k, 0) * max(k - s, 0) for k in strikes) for s in strikes}
    return min(pain, key=pain.get)


def flow_delta(near: List[Dict[str, Any]], before: Optional[Dict[float, Tuple[float, float]]]) -> Optional[float]:
    """Sum over near strikes: (PE OI change x |PE delta|) - (CE OI change x |CE delta|) vs 5 minutes ago."""
    if not before:
        return None
    flow = 0.0
    for r in near:
        b = before.get(float(r["strike"]))
        if not b:
            continue
        for leg, sign, idx in (("pe", 1, 1), ("ce", -1, 0)):
            node = r.get(leg) or {}
            oi, prev = node.get("oi"), b[idx]
            if oi is None or prev is None:
                continue
            flow += sign * (float(oi) - prev) * abs(float((node.get("greeks") or {}).get("delta") or 0))
    return flow


def wall_strike(rows: List[Dict[str, Any]], spot: float, entry: float, side: str, strikes_each_side: int = 10
                ) -> Optional[float]:
    """Highest same-side OI strike beyond the entry among ATM +/- strikes_each_side (backtest chain width)."""
    from .calculations import near_rows
    leg = "ce" if side == "CALL" else "pe"
    cand = [r for r in near_rows(rows, spot, strikes_each_side)
            if (r.get(leg) or {}).get("oi") is not None
            and (r["strike"] > entry if side == "CALL" else r["strike"] < entry)]
    return float(max(cand, key=lambda r: float(r[leg]["oi"] or 0))["strike"]) if cand else None


# --------------------------------------------------------------------------- features
def compute_features(side: str, *, now: datetime, entry: float, spot: float, ctx: Dict[str, Any],
                     candle5: Optional[Dict[str, float]], spot_candles: List[Dict[str, float]],
                     fut1m: List[Dict[str, float]], fut_ltp: Optional[float], vwap5: Optional[Dict[str, float]],
                     rows: List[Dict[str, Any]], near: List[Dict[str, Any]],
                     oi_before: Optional[Dict[float, Tuple[float, float]]], maxpain: Optional[float],
                     expiry: Optional[date], vix_open: Optional[float], bank_now: Optional[float],
                     bank_15: Optional[float], base_checks: Dict[str, Optional[bool]],
                     iv_now: Optional[float] = None, iv_before: Optional[float] = None) -> Dict[str, Optional[bool]]:
    """All 24 features (+ the v8 IV point) for one side; None = data not available (earns no point)."""
    sign = 1 if side == "CALL" else -1
    today = now.astimezone(IST).date()
    minute = now.hour * 60 + now.minute
    f: Dict[str, Optional[bool]] = {k: base_checks.get(k) for k in ("oi_unwinding", "vwap_band", "basis")}

    flow = flow_delta(near, oi_before)
    f["x_flow_delta"] = None if flow is None else sign * flow > 0
    # Same for both sides: a long option gains when IV holds or expands.
    f["x_iv_rising"] = None if not iv_now or not iv_before else iv_now - iv_before > IV_RISE_MIN
    vix_hist = ctx.get("vixOpens") or []
    f["f08_vix_low"] = (None if not vix_open or len(vix_hist) < 10
                        else vix_open < statistics.median(vix_hist[-20:]))
    f["f16_toward_maxpain"] = None if maxpain is None else sign * (maxpain - entry) > 0

    f["x_above_vwap"] = (None if fut_ltp is None or not vwap5 or vwap5.get("vwap") is None
                         else sign * (fut_ltp - vwap5["vwap"]) > 0)
    first = spot_candles[0] if spot_candles and spot_candles[0]["t"] == session_start(now) else None
    f["x_day_trend"] = None if first is None else sign * (entry - first["open"]) > 0
    orb30 = range_between(spot_candles, now, 555, 585)
    orb15 = range_between(spot_candles, now, 555, 570)

    def beyond(rng: Optional[Tuple[float, float]]) -> Optional[bool]:
        return None if rng is None else (entry > rng[0] if side == "CALL" else entry < rng[1])
    f["x_orb_break"] = f["f01_orb30_break"] = beyond(orb30)
    f["f01_orb15_break"] = beyond(orb15)
    prev = ctx.get("prevDay") or {}
    f["f02_day_dir"] = None if not prev.get("close") else sign * (spot - prev["close"]) > 0
    path = vwap_path(fut1m)
    if path:
        _, fnow, vw = path[-1]
        side_ok = sign * (fnow - vw) > 0
        recent = [p for p in path if p[0] >= now.timestamp() - 30 * 60]
        f["f03_vwap_side"] = side_ok
        f["f03_vwap_pullback"] = side_ok and any(sign * (p[1] - p[2]) <= 0.001 * p[2] for p in recent)
    else:
        f["f03_vwap_side"] = f["f03_vwap_pullback"] = None
    f["f06_prev_break"] = (None if not prev.get("high") or not prev.get("low")
                           else entry > prev["high"] if side == "CALL" else entry < prev["low"])
    if expiry is None:
        f["f09_dte_le1"] = f["f09_exp_pm"] = None
    else:
        f["f09_dte_le1"] = (expiry - today).days <= 1
        f["f09_exp_pm"] = expiry == today and minute >= 13 * 60
    if candle5 and rows:
        dist5 = max(abs(entry - (candle5["low"] if side == "CALL" else candle5["high"])), 0.05)
        wall = wall_strike(rows, spot, entry, side)
        f["f10_wall_room"] = wall is None or abs(wall - entry) >= 2 * dist5
    else:
        f["f10_wall_room"] = None
    client = ctx.get("clientChange")
    f["f17_client_contra"] = None if client is None else sign * client < 0
    f["f19_bank_15m"] = None if bank_now is None or bank_15 is None else sign * (bank_now - bank_15) > 0
    g = ctx.get("global") or {}

    def cue(name: str, flip: int = 1) -> Optional[bool]:
        v = g.get(name)
        return None if v is None else sign * flip * v > 0
    f["f22_es_overnight"] = cue("esOvernight")
    f["f22_spx_prev"] = cue("spx")
    f["f22_usvix_down"] = cue("usvix", -1)
    f["f22_inr_strong"] = cue("inr", -1)
    return f


# --------------------------------------------------------------------------- daily context
def parse_participant(text: str) -> Optional[float]:
    """Client net index futures (long - short) from an NSE participant-OI CSV."""
    rows = list(csv.reader(text.splitlines()))
    if len(rows) < 3:
        return None
    hdr = [h.strip() for h in rows[1]]
    for r in rows[2:]:
        if r and r[0].strip() == "Client":
            row = dict(zip(hdr, r))
            try:
                return float(row["Future Index Long"]) - float(row["Future Index Short"])
            except (KeyError, ValueError):
                return None
    return None


def last_before(series: List[Tuple[float, float]], cutoff: float) -> Optional[float]:
    v = None
    for t, c in series:
        if t <= cutoff:
            v = c
        else:
            break
    return v


def yahoo_series(payload: Dict[str, Any]) -> List[Tuple[float, float]]:
    res = ((payload.get("chart") or {}).get("result") or [None])[0] or {}
    closes = (((res.get("indicators") or {}).get("quote") or [{}])[0]).get("close") or []
    return [(float(t), float(c)) for t, c in zip(res.get("timestamp") or [], closes) if c is not None]


def daily_change(series: List[Tuple[float, float]], today: date) -> Optional[float]:
    """% change between the last two daily closes dated (UTC) before today."""
    s = [(t, c) for t, c in series if datetime.fromtimestamp(t, UTC).date() < today]
    return (s[-1][1] - s[-2][1]) / s[-2][1] * 100 if len(s) >= 2 and s[-2][1] else None


def es_overnight(series: List[Tuple[float, float]], prev_day: date, today: date) -> Optional[float]:
    """ES futures % move from the previous trading day 15:15 IST to today 09:15 IST."""
    a = last_before(series, datetime(prev_day.year, prev_day.month, prev_day.day, 9, 45, tzinfo=UTC).timestamp())
    b = last_before(series, datetime(today.year, today.month, today.day, 3, 45, tzinfo=UTC).timestamp())
    return (b - a) / a * 100 if a and b else None


class DailyContext:
    """Once-a-day inputs: previous day OHLC, VIX opens, NSE participant OI, global cues.

    Saved to ``<dir>/YYYY-MM-DD.json`` (kept forever for audits/backtests).
    """

    PARTS = ("prevDay", "vixOpens", "clientChange", "global")

    def __init__(self, directory: str) -> None:
        self.dir = directory
        self.day: Optional[str] = None
        self.data: Dict[str, Any] = {}
        self.errors: Dict[str, str] = {}
        self._next_try = 0.0
        self._task: Optional[asyncio.Task] = None
        self._warned: Optional[str] = None

    def complete(self) -> bool:
        g = self.data.get("global") or {}
        return (bool(self.data.get("prevDay")) and len(self.data.get("vixOpens") or []) >= 10
                and self.data.get("clientChange") is not None
                and all(g.get(k) is not None for k in ("esOvernight", "spx", "usvix", "inr")))

    def missing(self) -> List[str]:
        g = self.data.get("global") or {}
        out = [] if self.data.get("prevDay") else ["previous-day OHLC"]
        if len(self.data.get("vixOpens") or []) < 10:
            out.append("VIX history")
        if self.data.get("clientChange") is None:
            out.append("NSE participant OI")
        out += [f"Yahoo {k}" for k in ("esOvernight", "spx", "usvix", "inr") if g.get(k) is None]
        return out

    def _path(self, day: str) -> str:
        return os.path.join(self.dir, f"{day}.json")

    def _load(self, day: str) -> None:
        self.day, self.data, self.errors = day, {}, {}
        try:
            with open(self._path(day), "r", encoding="utf-8") as fh:
                saved = json.load(fh)
            self.data, self.errors = saved.get("data") or {}, saved.get("errors") or {}
        except (OSError, ValueError):
            pass

    def _save(self) -> None:
        try:
            os.makedirs(self.dir, exist_ok=True)
            tmp = self._path(self.day or "x") + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"day": self.day, "data": self.data, "errors": self.errors,
                           "savedAt": datetime.now(IST).isoformat(timespec="seconds")}, fh, indent=1)
            os.replace(tmp, self._path(self.day or "x"))
        except OSError:
            logger.warning("v5 context save failed", exc_info=True)

    def schedule(self, now: datetime) -> Optional[asyncio.Task]:
        day = now.date().isoformat()
        if self.day != day:
            self._load(day)
            self._next_try = 0.0
        if (self.complete() or now.hour * 60 + now.minute < CONTEXT_START_MIN or time.time() < self._next_try
                or (self._task and not self._task.done())):
            return None
        self._next_try = time.time() + CONTEXT_RETRY_SECONDS
        self._task = asyncio.create_task(self.refresh(now), name="algo2-v5-context")
        return self._task

    async def refresh(self, now: datetime) -> None:
        today = now.date()
        jobs = []
        if not self.data.get("prevDay") or len(self.data.get("vixOpens") or []) < 10:
            jobs.append(self._dhan_daily(today))
        if self.data.get("clientChange") is None:
            jobs.append(self._participant(today))
        g = self.data.get("global") or {}
        if any(g.get(k) is None for k in ("esOvernight", "spx", "usvix", "inr")):
            jobs.append(self._global(today))
        await asyncio.gather(*jobs, return_exceptions=True)
        self._save()

    async def _dhan_daily(self, today: date) -> None:
        from .market_data import parse_candles
        client = get_data_client()
        frm, to = (today - timedelta(days=45)).isoformat(), today.isoformat()
        try:
            if not self.data.get("prevDay"):
                raw = await client.get_historical_daily(str(NIFTY_ID), "IDX_I", "INDEX", frm, to)
                past = [c for c in parse_candles(raw) if ist_date(c["t"]) < today]
                if past:
                    p = past[-1]
                    self.data["prevDay"] = {"date": ist_date(p["t"]).isoformat(), "high": p["high"],
                                            "low": p["low"], "close": p["close"]}
            if len(self.data.get("vixOpens") or []) < 10:
                raw = await client.get_historical_daily(str(VIX_ID), "IDX_I", "INDEX", frm, to)
                self.data["vixOpens"] = [c["open"] for c in parse_candles(raw) if ist_date(c["t"]) < today][-20:]
            self.errors.pop("dhan", None)
        except DhanAPIError as exc:
            self.errors["dhan"] = exc.error_message
        except Exception as exc:  # noqa: BLE001
            self.errors["dhan"] = str(exc)

    async def _participant(self, today: date) -> None:
        found: List[float] = []
        try:
            async with httpx.AsyncClient(headers=_UA, timeout=15, follow_redirects=True) as http:
                d = today - timedelta(days=1)
                tries = 0
                while len(found) < 2 and tries < 12:
                    tries += 1
                    if d.weekday() < 5:
                        r = await http.get(NSE_ARCHIVE.format(d=d))
                        text = r.text if r.status_code == 200 else ""
                        value = parse_participant(text) if text and not text.lstrip().startswith("<") else None
                        if value is not None:
                            found.append(value)
                    d -= timedelta(days=1)
            if len(found) == 2:
                self.data["clientChange"] = found[0] - found[1]
                self.errors.pop("nse", None)
            else:
                self.errors["nse"] = f"participant OI files found: {len(found)}/2"
        except httpx.HTTPError as exc:
            self.errors["nse"] = f"{type(exc).__name__}: {exc}"

    async def _global(self, today: date) -> None:
        now_ts = int(time.time())
        g = dict(self.data.get("global") or {})
        prev_day = date.fromisoformat((self.data.get("prevDay") or {}).get("date") or
                                      (today - timedelta(days=3 if today.weekday() == 0 else 1)).isoformat())
        try:
            async with httpx.AsyncClient(headers=_UA, timeout=20) as http:
                async def get(sym: str, interval: str, days: int) -> List[Tuple[float, float]]:
                    r = await http.get(YAHOO_CHART.format(sym=sym), params={
                        "period1": now_ts - days * 86400, "period2": now_ts, "interval": interval})
                    return yahoo_series(r.json()) if r.status_code == 200 else []
                if g.get("esOvernight") is None:
                    g["esOvernight"] = es_overnight(await get("ES=F", "60m", 7), prev_day, today)
                for name, sym in (("spx", "^GSPC"), ("usvix", "^VIX"), ("inr", "INR=X")):
                    if g.get(name) is None:
                        g[name] = daily_change(await get(sym, "1d", 15), today)
            self.data["global"] = g
            missing = [k for k, v in g.items() if v is None]
            if missing:
                self.errors["yahoo"] = f"missing {missing}"
            else:
                self.errors.pop("yahoo", None)
        except (httpx.HTTPError, ValueError) as exc:
            self.data["global"] = g
            self.errors["yahoo"] = f"{type(exc).__name__}: {exc}"

    def warning(self, now: datetime) -> Optional[str]:
        """One warning per day once entries are near and some inputs are still missing."""
        day = now.date().isoformat()
        if self.complete() or self._warned == day or now.hour * 60 + now.minute < 9 * 60 + 40:
            return None
        self._warned = day
        return ("v5 score inputs missing: " + ", ".join(self.missing())
                + " - those features earn NO point (fewer trades, never extra ones).")

    def status(self) -> Dict[str, Any]:
        return {"day": self.day, "complete": self.complete(), "missing": self.missing(),
                "data": self.data, "errors": self.errors}
