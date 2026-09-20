"""Pure-Python technical indicators + helper stats.

No third-party TA library needed — keeps the backend light and dependency-free.
Every function is defensive: it returns `None` (or an empty list) rather than
raising when there isn't enough data, so the guidance engine can degrade
gracefully.

All price series are plain `list[float]` in chronological order (oldest first).
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence


# --------------------------------------------------------------------------
# Moving averages
# --------------------------------------------------------------------------
def sma(values: Sequence[float], period: int) -> Optional[float]:
    if period <= 0 or len(values) < period:
        return None
    window = values[-period:]
    return sum(window) / period


def ema_series(values: Sequence[float], period: int) -> List[float]:
    """Full EMA series. Empty if not enough data."""
    if period <= 0 or len(values) < period:
        return []
    k = 2.0 / (period + 1.0)
    # Seed with SMA of the first `period` values.
    seed = sum(values[:period]) / period
    out: List[float] = [seed]
    for v in values[period:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def ema(values: Sequence[float], period: int) -> Optional[float]:
    series = ema_series(values, period)
    return series[-1] if series else None


# --------------------------------------------------------------------------
# Momentum / oscillators
# --------------------------------------------------------------------------
def rsi(values: Sequence[float], period: int = 14) -> Optional[float]:
    """Wilder's RSI. Returns 0-100."""
    if len(values) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        change = values[i] - values[i - 1]
        if change >= 0:
            gains += change
        else:
            losses -= change
    avg_gain = gains / period
    avg_loss = losses / period
    for i in range(period + 1, len(values)):
        change = values[i] - values[i - 1]
        gain = max(change, 0.0)
        loss = max(-change, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 2)


def macd(
    values: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> Optional[Dict[str, float]]:
    """Returns {macd, signal, hist}. Histogram > 0 is bullish momentum."""
    if len(values) < slow + signal:
        return None
    ema_fast = ema_series(values, fast)
    ema_slow = ema_series(values, slow)
    # Align: ema_fast is longer; take the tail matching ema_slow length.
    ema_fast = ema_fast[-len(ema_slow):]
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
    signal_line = ema_series(macd_line, signal)
    if not signal_line:
        return None
    m = macd_line[-1]
    s = signal_line[-1]
    return {"macd": round(m, 3), "signal": round(s, 3), "hist": round(m - s, 3)}


def stochastic(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
) -> Optional[float]:
    """%K of the Stochastic oscillator (0-100)."""
    if len(closes) < period:
        return None
    hh = max(highs[-period:])
    ll = min(lows[-period:])
    if hh == ll:
        return 50.0
    k = (closes[-1] - ll) / (hh - ll) * 100.0
    return round(k, 2)


# --------------------------------------------------------------------------
# Volatility
# --------------------------------------------------------------------------
def true_range(high: float, low: float, prev_close: float) -> float:
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def atr(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14
) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    trs: List[float] = []
    for i in range(1, len(closes)):
        trs.append(true_range(highs[i], lows[i], closes[i - 1]))
    # Wilder smoothing
    a = sum(trs[:period]) / period
    for tr in trs[period:]:
        a = (a * (period - 1) + tr) / period
    return round(a, 3)


def bollinger(
    values: Sequence[float], period: int = 20, mult: float = 2.0
) -> Optional[Dict[str, float]]:
    if len(values) < period:
        return None
    window = values[-period:]
    mean = sum(window) / period
    var = sum((x - mean) ** 2 for x in window) / period
    sd = math.sqrt(var)
    return {
        "middle": round(mean, 2),
        "upper": round(mean + mult * sd, 2),
        "lower": round(mean - mult * sd, 2),
        "bandwidth": round((4 * sd) / mean * 100, 3) if mean else 0.0,
    }


def daily_returns(closes: Sequence[float]) -> List[float]:
    out: List[float] = []
    for i in range(1, len(closes)):
        if closes[i - 1]:
            out.append((closes[i] - closes[i - 1]) / closes[i - 1])
    return out


def realized_vol(closes: Sequence[float], annualize: bool = True) -> Optional[float]:
    """Standard deviation of returns, optionally annualized (x sqrt(252))."""
    rets = daily_returns(closes)
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    sd = math.sqrt(var)
    if annualize:
        sd *= math.sqrt(252)
    return round(sd * 100, 2)


# --------------------------------------------------------------------------
# Trend helpers
# --------------------------------------------------------------------------
def trend_label(closes: Sequence[float]) -> Dict[str, object]:
    """Simple multi-MA trend read used by the guidance engine."""
    price = closes[-1] if closes else None
    s20 = sma(closes, 20)
    s50 = sma(closes, 50)
    e9 = ema(closes, 9)
    e21 = ema(closes, 21)
    score = 0
    reasons: List[str] = []
    if price is not None and e9 is not None:
        if price > e9:
            score += 1
        else:
            score -= 1
    if e9 is not None and e21 is not None:
        if e9 > e21:
            score += 1
            reasons.append("EMA9 > EMA21 (short-term up)")
        else:
            score -= 1
            reasons.append("EMA9 < EMA21 (short-term down)")
    if s20 is not None and s50 is not None:
        if s20 > s50:
            score += 1
            reasons.append("SMA20 > SMA50 (medium-term up)")
        else:
            score -= 1
            reasons.append("SMA20 < SMA50 (medium-term down)")
    if score >= 2:
        label = "UP"
    elif score <= -2:
        label = "DOWN"
    else:
        label = "SIDEWAYS"
    return {"label": label, "score": score, "reasons": reasons}


def pct(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b in (None, 0):
        return None
    return round((a - b) / b * 100, 2)


# --------------------------------------------------------------------------
# Support / Resistance (pivot-based + swing-based) for an intraday read
# --------------------------------------------------------------------------
def classic_pivots(high: float, low: float, close: float) -> Dict[str, float]:
    """Classic floor-trader pivot levels from the previous session."""
    p = (high + low + close) / 3.0
    return {
        "pivot": round(p, 2),
        "r1": round(2 * p - low, 2),
        "r2": round(p + (high - low), 2),
        "r3": round(high + 2 * (p - low), 2),
        "s1": round(2 * p - high, 2),
        "s2": round(p - (high - low), 2),
        "s3": round(low - 2 * (high - p), 2),
    }


def swing_levels(
    highs: Sequence[float], lows: Sequence[float], price: float, lookback: int = 20
) -> Dict[str, List[float]]:
    """Nearest swing highs above price (resistance) and lows below (support)."""
    hs = list(highs[-lookback:]) if highs else []
    ls = list(lows[-lookback:]) if lows else []
    res = sorted({round(h, 2) for h in hs if h > price})
    sup = sorted({round(lo, 2) for lo in ls if lo < price}, reverse=True)
    return {"resistance": res[:3], "support": sup[:3]}


def trend_strength(closes: Sequence[float]) -> Optional[float]:
    """ADX-like 0-100 proxy from directional movement consistency."""
    if len(closes) < 15:
        return None
    ups = 0
    downs = 0
    for i in range(1, min(len(closes), 15)):
        if closes[-i] > closes[-i - 1]:
            ups += 1
        elif closes[-i] < closes[-i - 1]:
            downs += 1
    directional = abs(ups - downs) / max(1, ups + downs)
    return round(directional * 100, 1)
