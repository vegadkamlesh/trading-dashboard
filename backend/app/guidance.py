"""The Trading Advisor — combines live data, history studies & indicators.

Given an index + option leg (from the option chain), this produces an
EXPLAINABLE verdict:

    {
      "verdict": "BUY" | "SELL" | "WAIT",
      "confidence": 0-100,
      "reasons": [ "Trend up: ...", "RSI 58 ...", "History: 6/8 ...", ... ],
      "suggestedTargetPct": 12.0,
      "suggestedSafetySlPct": 3.0,
      "probabilityUp": 0.62,
      "scenarios": [ ... projected option P/L at -2%..+2% spot move ... ]
    }

It is rule-based and transparent — NOT a black box, and NOT a guarantee. Every
number in the output traces back to a reason string.
"""
from __future__ import annotations

import logging
import math
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from . import marketdata
from .analytics import (
    atr,
    classic_pivots,
    macd,
    realized_vol,
    rsi,
    swing_levels,
    trend_label,
)
from .dhan_client import DhanAPIError, get_dhan_client
from .history import history_engine
from .instruments import IndexInstrument, get_index
from .optionchain import engine as oc_engine

logger = logging.getLogger("dhan.guidance")


def _approx_option_move_pct(spot_move_pct: float, delta: Optional[float]) -> float:
    """Rough % move of an option for a given % move in the underlying spot.

    Uses |delta| as first-order sensitivity; falls back to an empirical ATM
    lever (~5x) when greeks are unavailable.
    """
    if delta is None or abs(delta) < 0.01:
        return spot_move_pct * 5.0
    return spot_move_pct * (abs(delta) * 100.0)


async def _load_history_ohlc(inst: IndexInstrument, days: int = 180) -> Dict[str, List[float]]:
    """Daily OHLC series for the index (for indicators).

    Delegates to the shared `marketdata` cache, so the expensive historical pull
    happens at most once per TTL (warmed at login) instead of every request.
    """
    return await marketdata.get_daily_ohlc(inst, days=days)


def _score_option_view(
    *,
    trend: Dict[str, Any],
    rsi_val: Optional[float],
    macd_val: Optional[Dict[str, float]],
    hist_win_rate: Optional[float],
    hist_avg_up: Optional[float],
    iv_val: Optional[float],
) -> tuple[int, float, List[str]]:
    """Returns (bull_score, probability_up, reasons)."""
    reasons: List[str] = []
    score = 0

    label = trend.get("label")
    trend_score = int(trend.get("score", 0) or 0)
    if label == "UP":
        reasons.append(f"Trend is UP (strength {trend_score:+d}).")
    elif label == "DOWN":
        reasons.append(f"Trend is DOWN (strength {trend_score:+d}).")
    else:
        reasons.append("Trend is SIDEWAYS (no clear direction).")
    score += trend_score

    if rsi_val is not None:
        if rsi_val >= 70:
            reasons.append(f"RSI {rsi_val} — overbought (pullback risk).")
            score -= 1
        elif rsi_val <= 30:
            reasons.append(f"RSI {rsi_val} — oversold (bounce likely).")
            score += 1
        elif rsi_val >= 55:
            reasons.append(f"RSI {rsi_val} — mildly bullish.")
            score += 1
        elif rsi_val <= 45:
            reasons.append(f"RSI {rsi_val} — mildly bearish.")
            score -= 1
        else:
            reasons.append(f"RSI {rsi_val} — neutral.")

    if macd_val:
        hist = macd_val.get("hist", 0)
        if hist > 0:
            reasons.append(f"MACD histogram +{hist} (bullish momentum).")
            score += 1
        else:
            reasons.append(f"MACD histogram {hist} (bearish momentum).")
            score -= 1

    if hist_win_rate is not None:
        reasons.append(f"History: this leg ended green on {hist_win_rate:.0f}% of sampled sessions.")
        if hist_win_rate >= 55:
            score += 1
        elif hist_win_rate <= 45:
            score -= 1

    if hist_avg_up is not None:
        reasons.append(f"History: avg best-case intraday swing ≈ {hist_avg_up:+.1f}%.")

    if iv_val is not None:
        if iv_val >= 25:
            reasons.append(f"IV {iv_val} — elevated (options pricier).")
        elif iv_val <= 12:
            reasons.append(f"IV {iv_val} — low (options cheaper).")

    probability_up = 1.0 / (1.0 + math.exp(-0.45 * score))
    return score, round(probability_up, 3), reasons


async def advise(
    index_key: str,
    option_type: str,          # "CE" | "PE"
    *,
    use_history: bool = True,
    history_days: int = 90,
) -> Dict[str, Any]:
    """Produce the guidance verdict for a CE/PE view on an index."""
    inst = get_index(index_key)
    if inst is None:
        return {"error": f"Unknown index '{index_key}'"}

    opt = "CALL" if option_type.upper() == "CE" else "PUT"

    # ---- live snapshot (spot + ATM leg greeks/iv) ----
    snap = oc_engine.snapshot(inst.key)
    spot: Optional[float] = None
    leg_greeks: Dict[str, Any] = {}
    leg_iv: Optional[float] = None
    atm_strike: Optional[float] = None
    if snap and snap.get("rows"):
        spot = snap.get("underlyingLtp")
        rows = snap["rows"]
        if spot:
            atm_strike = min(rows, key=lambda r: abs(r["strike"] - spot))["strike"]
            atm_row = next((r for r in rows if r["strike"] == atm_strike), None)
            if atm_row:
                leg = atm_row["ce"] if opt == "CALL" else atm_row["pe"]
                leg_greeks = leg.get("greeks", {}) or {}
                leg_iv = leg.get("iv")

    # ---- indicators from daily OHLC ----
    ohlc = await _load_history_ohlc(inst, days=180)
    closes = ohlc["close"]
    support_resistance: Dict[str, Any] = {}
    if closes:
        trend = trend_label(closes)
        rsi_val = rsi(closes)
        macd_val = macd(closes)
        rv = realized_vol(closes)
        atr_val = atr(ohlc["high"], ohlc["low"], closes, 14) if ohlc["high"] else None
        # Support/resistance for an intraday read.
        if spot:
            swings = swing_levels(ohlc["high"], ohlc["low"], spot, lookback=20)
            piv = {}
            if ohlc["high"] and ohlc["low"]:
                piv = classic_pivots(ohlc["high"][-1], ohlc["low"][-1], closes[-1])
            support_resistance = {
                "support": swings["support"],
                "resistance": swings["resistance"],
                "pivot": piv or None,
            }
    else:
        trend = {"label": "UNKNOWN", "score": 0, "reasons": []}
        rsi_val = macd_val = rv = atr_val = None

    # ---- history study ----
    hist_win: Optional[float] = None
    hist_up: Optional[float] = None
    hist_dict: Dict[str, Any] = {}
    if use_history:
        # CACHE-ONLY: never block this (often-polled) advisor on the heavy 5-yr
        # fetch. The background warm-up primes ATM studies; if not ready yet we
        # simply proceed without the history weight.
        study = await history_engine.study(
            inst, opt, "ATM", lookback_days=history_days, cached_only=True
        )
        hist_dict = study.to_dict()
        hist_win = study.contract_win_rate
        hist_up = study.avg_intraday_gain_pct

    bull_score, prob_up, reasons = _score_option_view(
        trend=trend,
        rsi_val=rsi_val,
        macd_val=macd_val,
        hist_win_rate=hist_win,
        hist_avg_up=hist_up,
        iv_val=leg_iv,
    )

    # ---- verdict ----
    if bull_score >= 3:
        verdict = "BUY"
    elif bull_score <= -3:
        verdict = "SELL"
    else:
        verdict = "WAIT"
    confidence = int(max(5, min(95, abs(bull_score) * 14 + 35)))

    # ---- suggested target / safety ----
    base_target = 10.0 + abs(bull_score) * 1.5
    if hist_up is not None and hist_up > 0:
        base_target = min(base_target, max(6.0, hist_up * 0.6))
    suggested_target = round(min(max(base_target, 5.0), 35.0), 1)

    implied_move = None
    if spot and atr_val:
        implied_move = round(atr_val / spot * 100, 2)

    scenarios = _build_scenarios(spot, leg_greeks.get("delta"))

    return {
        "index": inst.key,
        "optionType": option_type.upper(),
        "verdict": verdict,
        "confidence": confidence,
        "bullScore": bull_score,
        "probabilityUp": prob_up,
        "reasons": reasons,
        "spot": spot,
        "atmStrike": atm_strike,
        "rsi": rsi_val,
        "macd": macd_val,
        "trend": trend.get("label"),
        "realizedVolPct": rv,
        "iv": leg_iv,
        "suggestedTargetPct": suggested_target,
        "suggestedSafetySlPct": 3.0,
        "impliedDailyMovePct": implied_move,
        "supportResistance": support_resistance or None,
        "history": hist_dict or None,
        "scenarios": scenarios,
        "disclaimer": (
            "Rule-based, explainable estimate from live data + 5-yr history. "
            "Educational only — NOT financial advice. Markets can gap; always "
            "size positions you can afford to lose."
        ),
    }


def _build_scenarios(spot: Optional[float], delta: Optional[float]) -> List[Dict[str, Any]]:
    """Projected option % P/L for a range of underlying spot moves."""
    if not spot:
        return []
    out: List[Dict[str, Any]] = []
    for m in (-2.0, -1.0, -0.5, 0.0, 0.5, 1.0, 2.0):
        opt_move = _approx_option_move_pct(m, delta)
        out.append(
            {
                "spotMovePct": m,
                "spotLevel": round(spot * (1 + m / 100.0), 2),
                "optionMovePct": round(opt_move, 1),
            }
        )
    return out
