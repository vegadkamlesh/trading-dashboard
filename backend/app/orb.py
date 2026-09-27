"""Opening-Range Breakout (ORB) backtest + parameter research.

Strategy simulated (1-minute bars, long-only via options):
  * OR = HIGH/LOW of 09:15-09:25.
  * After 09:25 the FIRST boundary crossed gives the signal:
      - spot breaks OR HIGH -> BUY CALL (bullish)
      - spot breaks OR LOW  -> BUY PUT  (bearish)
  * Entry at the breakout level (+ optional slippage).
  * Stop-loss / target = configurable (see StrategyConfig).
  * Whichever is touched first wins; else exit at 15:15 (TIME).

Option P&L is approximated: premium move ~= delta x spot move (1 lot). Real
option P&L also carries theta/IV, so treat rupee figures as indicative.

Pure computation over already-fetched candles; no network.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from .opening_range import WINDOW_END_MIN, WINDOW_START_MIN, _f

EXIT_CUTOFF_MIN = 15 * 60 + 15  # square-off minute


@dataclass
class Bar:
    t: int
    o: float
    h: float
    l: float
    c: float


@dataclass
class TargetRule:
    name: str
    kind: str          # "points" | "range_mult" | "pct"
    value: float


@dataclass
class StrategyConfig:
    """Full strategy knob-set so we can sweep it in a backtest."""

    target: TargetRule
    # Stop-loss:
    #   "opposite"   -> other OR boundary (risk = the whole OR range) [your rule]
    #   "range_mult" -> entry -/+ value * OR-range (tighter stop)
    sl_kind: str = "opposite"
    sl_value: float = 1.0
    # Only take breakouts before this minute-of-day (e.g. 10:30). None = any time.
    max_entry_min: Optional[int] = None
    # Move the stop to break-even once price runs +value * OR-range in favour.
    breakeven_at: Optional[float] = None
    # Volatility filter: only trade when OR range is within [min_or_pct, max_or_pct]%
    min_or_pct: Optional[float] = None
    max_or_pct: Optional[float] = None

    def label(self) -> str:
        return self.target.name


@dataclass
class Trade:
    d: date
    index: str
    direction: str
    or_high: float
    or_low: float
    or_range: float
    entry_t: int
    entry: float
    target: float
    stop: float
    exit_t: int
    exit: float
    exit_reason: str   # TARGET | SL | TRAIL | TIME
    points: float
    mfe: float
    mae: float
    r_multiple: float = 0.0  # points / initial-risk

    def to_dict(self) -> Dict[str, Any]:
        return {
            "date": self.d.isoformat(), "index": self.index, "direction": self.direction,
            "orHigh": round(self.or_high, 2), "orLow": round(self.or_low, 2),
            "orRange": round(self.or_range, 2), "entryTime": _fmt_min(self.entry_t),
            "entry": round(self.entry, 2), "target": round(self.target, 2),
            "stop": round(self.stop, 2), "exitTime": _fmt_min(self.exit_t),
            "exit": round(self.exit, 2), "exitReason": self.exit_reason,
            "points": round(self.points, 2), "mfe": round(self.mfe, 2),
            "mae": round(self.mae, 2), "rMultiple": round(self.r_multiple, 2),
        }


def _fmt_min(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


# ---------------------------------------------------------------- parsing ----

def parse_bars(raws: List[Any]) -> Dict[date, List[Bar]]:
    days: Dict[date, List[Bar]] = {}
    for raw in raws:
        data = raw.get("data") if isinstance(raw, dict) and "data" in raw else raw
        if not isinstance(data, dict):
            continue
        o = data.get("open") or []
        h = data.get("high") or []
        lo = data.get("low") or []
        c = data.get("close") or []
        ts = data.get("timestamp") or []
        n = min(len(o), len(h), len(lo), len(c), len(ts))
        for i in range(n):
            try:
                dt = datetime.fromtimestamp(int(ts[i]))
            except (TypeError, ValueError, OverflowError, OSError):
                continue
            ov, hv, lv, cv = _f(o[i]), _f(h[i]), _f(lo[i]), _f(c[i])
            if None in (ov, hv, lv, cv):
                continue
            days.setdefault(dt.date(), []).append(
                Bar(t=dt.hour * 60 + dt.minute, o=ov, h=hv, l=lv, c=cv)  # type: ignore[arg-type]
            )
    for d in days:
        days[d].sort(key=lambda b: b.t)
    return days


# --------------------------------------------------------------- simulate ----

def compute_target(entry: float, rng: float, rule: TargetRule, direction: str) -> float:
    if rule.kind == "points":
        delta = rule.value
    elif rule.kind == "range_mult":
        delta = rule.value * rng
    else:
        delta = entry * rule.value / 100.0
    return entry + delta if direction == "CALL" else entry - delta


def simulate_day(
    bars: List[Bar],
    cfg: StrategyConfig,
    index_key: str,
    tgt_date: date,
    slippage_pts: float = 0.0,
) -> Optional[Trade]:
    or_bars = [b for b in bars if WINDOW_START_MIN <= b.t <= WINDOW_END_MIN]
    if len(or_bars) < 2:
        return None
    or_high = max(b.h for b in or_bars)
    or_low = min(b.l for b in or_bars)
    rng = or_high - or_low
    if rng <= 0 or or_high <= 0:
        return None

    # Volatility filter (skip days whose OR is unusually small / huge).
    or_pct = rng / or_high * 100.0
    if cfg.min_or_pct is not None and or_pct < cfg.min_or_pct:
        return None
    if cfg.max_or_pct is not None and or_pct > cfg.max_or_pct:
        return None

    post = [b for b in bars if WINDOW_END_MIN < b.t <= EXIT_CUTOFF_MIN]
    if cfg.max_entry_min is not None:
        post = [b for b in post if b.t <= cfg.max_entry_min]
    if not post:
        return None

    direction = entry = None
    entry_t = start_idx = None
    for idx, b in enumerate(post):
        up = b.h > or_high
        dn = b.l < or_low
        if up and dn:
            if (or_high - b.o) <= (b.o - or_low):
                dn = False
            else:
                up = False
        if up:
            direction, entry, entry_t, start_idx = "CALL", max(or_high, b.o), b.t, idx
            break
        if dn:
            direction, entry, entry_t, start_idx = "PUT", min(or_low, b.o), b.t, idx
            break
    if direction is None or entry is None or entry_t is None or start_idx is None:
        return None

    entry = entry + slippage_pts if direction == "CALL" else entry - slippage_pts

    target = compute_target(entry, rng, cfg.target, direction)
    if cfg.sl_kind == "range_mult":
        risk = cfg.sl_value * rng
        stop = entry - risk if direction == "CALL" else entry + risk
    else:  # opposite OR boundary (your original rule)
        stop = or_low if direction == "CALL" else or_high
        risk = (entry - stop) if direction == "CALL" else (stop - entry)
    if risk <= 0:
        return None

    trail_stop = stop
    mfe = mae = 0.0
    exit_price, exit_reason, exit_t = None, "TIME", post[-1].t

    for b in post[start_idx + 1:]:
        if direction == "CALL":
            mfe = max(mfe, b.h - entry)
            mae = max(mae, entry - b.l)
            hit_sl = b.l <= trail_stop
            hit_tgt = b.h >= target
        else:
            mfe = max(mfe, entry - b.l)
            mae = max(mae, b.h - entry)
            hit_sl = b.h >= trail_stop
            hit_tgt = b.l <= target
        if hit_sl:
            exit_price = trail_stop
            exit_reason = "SL" if trail_stop == stop else "TRAIL"
            exit_t = b.t
            break
        if hit_tgt:
            exit_price, exit_reason, exit_t = target, "TARGET", b.t
            break
        # Move the stop to break-even once far enough in profit.
        if cfg.breakeven_at is not None and mfe >= cfg.breakeven_at * rng:
            if direction == "CALL" and trail_stop < entry:
                trail_stop = entry
            elif direction == "PUT" and trail_stop > entry:
                trail_stop = entry

    if exit_price is None:
        exit_price, exit_reason, exit_t = post[-1].c, "TIME", post[-1].t

    points = exit_price - entry if direction == "CALL" else entry - exit_price
    return Trade(
        d=tgt_date, index=index_key, direction=direction,
        or_high=or_high, or_low=or_low, or_range=rng,
        entry_t=entry_t, entry=entry, target=target, stop=stop,
        exit_t=exit_t, exit=exit_price, exit_reason=exit_reason,
        points=points, mfe=mfe, mae=mae,
        r_multiple=points / risk if risk else 0.0,
    )


def collect_trades(
    bars_by_day: Dict[date, List[Bar]],
    cfg: StrategyConfig,
    index_key: str,
    slippage_pts: float = 0.0,
) -> List[Trade]:
    out: List[Trade] = []
    for d in sorted(bars_by_day):
        tr = simulate_day(bars_by_day[d], cfg, index_key, d, slippage_pts)
        if tr is not None:
            out.append(tr)
    return out


# -------------------------------------------------------------- summarize ----

def max_drawdown(points_seq: List[float]) -> Tuple[float, float]:
    cum = peak = 0.0
    mdd = 0.0
    for p in points_seq:
        cum += p
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
    return round(-mdd, 2), round(cum, 2)


def summarize(trades: List[Trade], lot_size: int, delta: float = 0.5) -> Dict[str, Any]:
    if not trades:
        return {"trades": 0}
    wins = [t for t in trades if t.points > 0]
    losses = [t for t in trades if t.points <= 0]
    seq = [t.points for t in sorted(trades, key=lambda x: (x.d, x.entry_t))]
    net = sum(seq)
    gross_win = sum(t.points for t in wins)
    gross_loss = -sum(t.points for t in losses)
    mdd, _ = max_drawdown(seq)
    reasons: Dict[str, int] = {}
    for t in trades:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
    calls = sum(1 for t in trades if t.direction == "CALL")
    avg_win = statistics.mean([t.points for t in wins]) if wins else 0.0
    avg_loss = statistics.mean([t.points for t in losses]) if losses else 0.0
    r_vals = [t.r_multiple for t in trades]
    rupees_per_pt = delta * lot_size
    return {
        "trades": len(trades), "calls": calls, "puts": len(trades) - calls,
        "wins": len(wins), "losses": len(losses),
        "winRate": round(len(wins) / len(trades) * 100, 1),
        "targetHits": reasons.get("TARGET", 0), "slHits": reasons.get("SL", 0),
        "trailExits": reasons.get("TRAIL", 0), "timeExits": reasons.get("TIME", 0),
        "netPoints": round(net, 1), "avgPoints": round(net / len(trades), 2),
        "avgWinPoints": round(avg_win, 2), "avgLossPoints": round(avg_loss, 2),
        "rr": round(abs(avg_win / avg_loss), 2) if avg_loss else None,
        "expectancyR": round(statistics.mean(r_vals), 3) if r_vals else None,
        "grossWinPoints": round(gross_win, 1), "grossLossPoints": round(gross_loss, 1),
        "profitFactor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "maxDrawdownPoints": mdd,
        "avgOrRange": round(statistics.mean([t.or_range for t in trades]), 1),
        "avgMfe": round(statistics.mean([t.mfe for t in trades]), 1),
        "avgMae": round(statistics.mean([t.mae for t in trades]), 1),
        "netRupeesPerLot": round(net * rupees_per_pt, 0),
        "maxDrawdownRupeesPerLot": round(mdd * rupees_per_pt, 0),
        "rupeesPerPoint": round(rupees_per_pt, 2),
    }


def combined_one_trade_per_day(per_index_trades: Dict[str, List[Trade]]) -> List[Trade]:
    by_day: Dict[date, Trade] = {}
    for idx, trades in per_index_trades.items():
        for t in trades:
            cur = by_day.get(t.d)
            if cur is None or (t.entry_t, idx) < (cur.entry_t, cur.index):
                by_day[t.d] = t
    return [by_day[d] for d in sorted(by_day)]


def apply_cost(trades: List[Trade], cost_pts: float) -> List[Trade]:
    """Deduct a round-trip cost (slippage + brokerage, in spot points) per trade."""
    if not cost_pts:
        return trades
    out: List[Trade] = []
    for t in trades:
        t2 = replace(t, points=t.points - cost_pts)
        out.append(t2)
    return out


def net_rupees(trades: List[Trade], lot_by_index: Dict[str, int], delta: float) -> Tuple[float, float]:
    """(net rupees, max drawdown rupees) using each trade's own index lot size."""
    seq = sorted(trades, key=lambda x: (x.d, x.entry_t))
    cum = peak = 0.0
    mdd = 0.0
    for t in seq:
        cum += t.points * delta * lot_by_index.get(t.index, 1)
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
    return round(cum, 0), round(-mdd, 0)


def mfe_distribution(
    trades: List[Trade], pcts: Tuple[float, ...] = (30, 50, 60, 70, 80, 90)
) -> Dict[str, Any]:
    if not trades:
        return {}
    mult = sorted(t.mfe / t.or_range for t in trades if t.or_range > 0)
    out: Dict[str, Any] = {"samples": len(mult)}
    for p in pcts:
        i = min(len(mult) - 1, int(round(p / 100 * (len(mult) - 1))))
        out[f"p{int(p)}"] = round(mult[i], 2)
    out["mean"] = round(statistics.mean(mult), 2)
    out["median"] = round(statistics.median(mult), 2)
    return out
