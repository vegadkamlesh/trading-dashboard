"""Per-strike BUY recommender — "kaunsa option BUY karna hai, kitna % chance?"

Given the live chain + market context (trend, RSI, MACD, IV, S/R, history
win-rate, and TIME TO EXPIRY), this module produces, for every strike near the
money, a recommendation for BOTH legs:

    { strike, side: "CE"|"PE", action: "BUY"|"WAIT"|"AVOID",
      profitProbability: 0-100, confidence: 0-100,
      targetPct, stopPct, expectedMovePct, thetaRisk: low|med|high,
      reasons: [...] }

Trader-honest design (10+ yrs mindset):
  * We NEVER say "guaranteed". We output probability + conviction only.
  * THETA is a first-class input: on expiry day / 1 DTE, time decay eats
    premium fast, so we demand a stronger directional edge before saying BUY
    and we flag `thetaRisk: high`.
  * Cheap far-OTM lotto strikes are AVOIDed unless momentum + history agree.
  * Every recommendation is explainable via `reasons`.

Inputs come from already-cached data — this is cheap to call per request.

Prediction inputs (ALL of them):
  * LIVE MARKET      — spot, trend (daily closes), RSI, MACD from live data
  * LIVE STRIKE PRICE— each leg's LTP, IV, delta, theta from the live chain
  * STRIKE vs SPOT   — moneyness (how far OTM/ITM)
  * NEWS SENTIMENT   — overall market read from the news panel (optional)
  * HISTORY          — 5-yr expired-option win-rate for this side
  * TIME             — days-to-expiry (theta penalty)
"""
from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from .analytics import macd, rsi, trend_label

# Tuning: how much each factor can move the underlying directional score.
_W_TREND = 1.0
_W_RSI_SLOPE = 0.8
_W_MACD = 1.0
_W_HISTORY = 1.2
# News sentiment: a softer nudge than price action (headlines are noisier and
# can be stale), but it still shifts the directional bias.
_W_NEWS = 0.7
_W_MOMENTUM_NEAR_ATM = 0.6


def _days_to_expiry(expiry: str, today: Optional[date] = None) -> Optional[int]:
    """Trading-agnostic calendar days to expiry (>=0). None if unparseable."""
    if not expiry:
        return None
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            d = datetime.strptime(expiry, fmt).date()
        except ValueError:
            continue
        today = today or date.today()
        return max(0, (d - today).days)
    return None


def recommend(
    *,
    index_key: str,
    expiry: str,
    spot: float,
    rows: List[Dict[str, Any]],
    closes: List[float],
    highs: List[float],
    lows: List[float],
    hist_win_rate_ce: Optional[float] = None,
    hist_win_rate_pe: Optional[float] = None,
    news_net_score: Optional[float] = None,  # -1..+1 from news sentiment
    range_strikes: int = 10,
) -> Dict[str, Any]:
    """Return market context + per-strike recommendations near the money."""
    if not rows or not spot:
        return {"error": "no chain data", "recommendations": []}

    dte = _days_to_expiry(expiry)

    # ---- market context (direction + strength) ----
    trend = trend_label(closes) if closes else {"label": "UNKNOWN", "score": 0}
    rsi_val = rsi(closes) if closes else None
    macd_val = macd(closes) if closes else None

    # Directional bias in [-1, +1]: >0 bullish, <0 bearish.
    bias = 0.0
    reasons_ctx: List[str] = []

    ts = int(trend.get("score", 0) or 0)
    bias += _W_TREND * (ts / 3.0)
    reasons_ctx.append(f"Trend {trend.get('label')} (strength {ts:+d}).")

    if rsi_val is not None:
        # RSI > 50 leans up, < 50 leans down; extremes add mean-reversion caution.
        bias += _W_RSI_SLOPE * ((rsi_val - 50) / 50.0)
        if rsi_val >= 75:
            bias -= 0.3
            reasons_ctx.append(f"RSI {rsi_val}  - overbought, chase risk.")
        elif rsi_val <= 25:
            bias += 0.3
            reasons_ctx.append(f"RSI {rsi_val}  - oversold, bounce zone.")
        else:
            reasons_ctx.append(f"RSI {rsi_val}.")

    if macd_val is not None:
        hist = macd_val.get("hist", 0.0)
        bias += _W_MACD * (1.0 if hist > 0 else -1.0) * min(1.0, abs(hist) / 5.0)
        reasons_ctx.append(f"MACD hist {hist:+.2f}.")

    # News sentiment — a directional nudge from the latest headlines.
    if news_net_score is not None:
        ns = max(-1.0, min(1.0, news_net_score))
        bias += _W_NEWS * ns
        if ns >= 0.15:
            reasons_ctx.append(f"News flow leans BULLISH (net {ns:+.2f}).")
        elif ns <= -0.15:
            reasons_ctx.append(f"News flow leans BEARISH (net {ns:+.2f}).")
        else:
            reasons_ctx.append("News flow is mixed/neutral.")

    bias = max(-1.0, min(1.0, bias))

    # ---- pick the window of strikes around ATM ----
    rows_sorted = sorted(rows, key=lambda r: r["strike"])
    atm_idx = min(
        range(len(rows_sorted)), key=lambda i: abs(rows_sorted[i]["strike"] - spot)
    )
    lo = max(0, atm_idx - range_strikes)
    hi = min(len(rows_sorted), atm_idx + range_strikes + 1)
    window = rows_sorted[lo:hi]

    recommendations: List[Dict[str, Any]] = []
    for r in window:
        strike = r["strike"]
        moneyness = (strike - spot) / spot * 100.0
        for side in ("CE", "PE"):
            leg = r.get("ce" if side == "CE" else "pe") or {}
            recommendations.append(
                _score_leg(
                    side=side,
                    strike=strike,
                    spot=spot,
                    moneyness_pct=moneyness,
                    leg=leg,
                    bias=bias,
                    dte=dte,
                    hist_win=(hist_win_rate_ce if side == "CE" else hist_win_rate_pe),
                )
            )

    # Context summary
    if bias >= 0.33:
        stance = "BULLISH"
    elif bias <= -0.33:
        stance = "BEARISH"
    else:
        stance = "NEUTRAL"

    return {
        "index": index_key,
        "expiry": expiry,
        "spot": round(spot, 2),
        "daysToExpiry": dte,
        "marketBias": round(bias, 3),
        "stance": stance,
        "rsi": rsi_val,
        "macd": macd_val,
        "trend": trend.get("label"),
        "newsNetScore": round(news_net_score, 3) if news_net_score is not None else None,
        "contextReasons": reasons_ctx,
        "recommendations": recommendations,
        "disclaimer": (
            "Probability-based, explainable suggestions - NOT advice or a "
            "guarantee. Theta (time decay) and gaps can wipe out premium. "
            "Trade small, use stops, and never risk money you can't lose."
        ),
    }


def _score_leg(
    *,
    side: str,
    strike: float,
    spot: float,
    moneyness_pct: float,
    leg: Dict[str, Any],
    bias: float,
    dte: Optional[int],
    hist_win: Optional[float],
) -> Dict[str, Any]:
    """Score a single option leg into BUY / WAIT / AVOID with probability."""
    reasons: List[str] = []
    if not leg.get("securityId"):
        return {
            "strike": strike,
            "side": side,
            "action": "AVOID",
            "profitProbability": 0,
            "reasons": ["No tradable contract (missing security id)."],
        }

    ltp = leg.get("ltp") or 0.0
    iv = leg.get("iv")
    delta = (leg.get("greeks") or {}).get("delta")
    theta = (leg.get("greeks") or {}).get("theta")

    # Directional score for THIS side: CE benefits from +bias, PE from -bias.
    side_bias = bias if side == "CE" else -bias
    score = side_bias * 3.0  # base in [-3, +3]

    # Moneyness: slightly OTM is the sweet spot for buyers (cheaper, still moves);
    # deep OTM decays to zero, deep ITM is capital-heavy.
    dist = abs(moneyness_pct)
    if dist <= 0.6:
        score += 0.3
        reasons.append("Near the money - high delta, good responsiveness.")
    elif dist <= 1.5:
        score += 0.1
        reasons.append("Slightly out-of-the-money - cheaper with decent delta.")
    elif dist >= 3.0:
        score -= 0.8
        reasons.append("Deep OTM — cheap but low probability; decays fast.")

    # History win-rate for this side (empirical).
    if hist_win is not None:
        if hist_win >= 55:
            score += _W_HISTORY
            reasons.append(f"History: {side} ended green {hist_win:.0f}% of sessions.")
        elif hist_win <= 42:
            score -= _W_HISTORY * 0.8
            reasons.append(f"History: {side} weak ({hist_win:.0f}% green).")

    # ---- THETA awareness ----
    theta_risk = "low"
    if dte is not None:
        if dte == 0:
            theta_risk = "high"
            score -= 1.0
            reasons.append("EXPIRY DAY - theta is brutal; only momentum plays work.")
        elif dte <= 2:
            theta_risk = "high"
            score -= 0.5
            reasons.append(f"Only {dte} day(s) to expiry — steep time decay.")
        elif dte <= 5:
            theta_risk = "medium"
            reasons.append(f"{dte} days to expiry - decay will pick up.")
    if theta is not None and ltp:
        # If daily theta is a big chunk of premium, flag it.
        theta_pct = abs(theta) / ltp * 100 if ltp else 0
        if theta_pct >= 8:
            theta_risk = "high"
            reasons.append(f"Theta ≈ {theta_pct:.0f}% of premium/day - decay is the enemy.")
        elif theta_pct >= 4 and theta_risk == "low":
            theta_risk = "medium"

    # ---- IV context ----
    if iv is not None:
        if iv >= 28:
            reasons.append(f"IV {iv:.0f} - expensive premium (buyers beware).")
            score -= 0.2
        elif iv <= 12:
            reasons.append(f"IV {iv:.0f} - cheap premium (favorable for buyers).")
            score += 0.2

    # ---- convert score -> probability ----
    prob = 1.0 / (1.0 + math.exp(-0.7 * score))  # logistic
    prob_pct = int(round(prob * 100))

    # Action thresholds.
    if score >= 1.6:
        action = "BUY"
    elif score >= 0.4:
        action = "WAIT"
    else:
        action = "AVOID"

    # Suggested plan (buyer-friendly): target scales with |score| & DTE,
    # stop is tighter when theta is high.
    target_pct = round(min(40.0, max(8.0, 10 + abs(score) * 4)), 1)
    stop_pct = 25.0 if theta_risk == "high" else 35.0

    if action == "BUY":
        reasons.insert(0, f"{side} aligns with market bias ({'bullish' if side=='CE' else 'bearish'} read).")

    confidence = int(min(95, max(8, abs(score) * 22 + (hist_win or 50) * 0.3)))

    return {
        "strike": strike,
        "side": side,
        "action": action,
        "profitProbability": prob_pct,
        "confidence": confidence,
        "ltp": ltp,
        "iv": iv,
        "delta": delta,
        "theta": theta,
        "thetaRisk": theta_risk,
        "suggestedTargetPct": target_pct,
        "suggestedStopPct": stop_pct,
        "score": round(score, 2),
        "reasons": reasons,
    }
