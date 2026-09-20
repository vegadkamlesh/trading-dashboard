"""Headline sentiment classifier for market news.

Goal: instead of reading 50 headlines, the trader sees a fast, explainable
tag per headline — BULLISH / BEARISH / NEUTRAL — plus an impact score and the
matched keywords ("why").

Design
------
* Fully LOCAL and dependency-free: a finance-tuned keyword lexicon with weights.
  This runs instantly for every headline, needs no API key, and never leaks data.
* EXPLAINABLE: we return the exact terms that drove the score, so the rank makes
  sense to a human (a 10-yr trader trusts reasons, not a black box).
* Optional LLM upgrade: if `APP_LLM_API_KEY` is set (OpenAI-compatible), an
  `llm_sentiment()` helper can enrich borderline cases. It is OFF by default and
  never required — the app works perfectly without it.

This is a *prioritisation* aid, NOT a trade signal on its own.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("dhan.sentiment")

# --------------------------------------------------------------------------
# Lexicon: term -> weight (positive = bullish for the market, negative = bearish)
# Phrases are matched case-insensitively on word boundaries where sensible.
# --------------------------------------------------------------------------
BULLISH_TERMS: Dict[str, float] = {
    "surge": 2.0, "surges": 2.0, "soar": 2.0, "soars": 2.0, "rally": 1.8,
    "rallies": 1.8, "jump": 1.5, "jumps": 1.5, "gains": 1.2, "gain": 1.2,
    "rise": 1.0, "rises": 1.0, "climb": 1.0, "climbs": 1.0, "up": 0.6,
    "higher": 1.0, "record high": 2.5, "all-time high": 2.5, "lifetime high": 2.5,
    "bullish": 2.0, "buy": 1.0, "buying": 1.0, "outperform": 1.6, "upgrade": 1.5,
    "positive": 1.0, "growth": 1.0, "strong": 1.2, "beat": 1.3, "beats": 1.3,
    "profit": 1.0, "profits": 1.0, "recovery": 1.2, "rebound": 1.5, "bounce": 1.3,
    "optimism": 1.3, "optimistic": 1.3, "cheer": 1.0, "boost": 1.2, "upside": 1.2,
    "breakout": 1.6, "top gainers": 1.5, "nifty up": 1.5, "sensex up": 1.5,
    "inflows": 1.3, "fii buying": 1.8, "dii buying": 1.3, "rate cut": 2.0,
    "stimulus": 1.8, "easing": 1.2, "dividend": 0.8, "expansion": 1.0,
}

BEARISH_TERMS: Dict[str, float] = {
    "fall": 1.0, "falls": 1.0, "drop": 1.2, "drops": 1.2, "slump": 2.0,
    "slumps": 2.0, "plunge": 2.2, "plunges": 2.2, "crash": 2.8, "crashes": 2.8,
    "tumble": 1.8, "tumbles": 1.8, "decline": 1.2, "declines": 1.2, "down": 0.6,
    "lower": 1.0, "bearish": 2.0, "sell": 1.0, "selling": 1.0, "selloff": 2.0,
    "sell-off": 2.0, "downgrade": 1.5, "underperform": 1.6, "weak": 1.2,
    "loss": 1.2, "losses": 1.2, "recession": 2.2, "fear": 1.5, "fears": 1.5,
    "panic": 2.2, "worry": 1.3, "worries": 1.3, "concern": 1.0, "concerns": 1.0,
    "jitters": 1.5, "correction": 1.3, "pressure": 1.0, "drag": 1.0, "slip": 1.0,
    "slips": 1.0, "top losers": 1.5, "nifty down": 1.5, "sensex down": 1.5,
    "outflows": 1.3, "fii selling": 1.8, "dii selling": 1.3, "rate hike": 2.0,
    "inflation": 1.0, "hawkish": 1.5, "default": 1.8, "bankruptcy": 2.0,
    "warning": 1.2, "warns": 1.2, "cut": 0.6, "slashes": 1.4, "miss": 1.2,
    "misses": 1.2, "disappoint": 1.4, "disappoints": 1.4,
}

# Words that make a headline market-relevant / high impact (boost magnitude).
HIGH_IMPACT = {
    "rbi": 1.5, "fed": 1.5, "federal reserve": 1.5, "budget": 1.4, "gdp": 1.4,
    "inflation": 1.3, "crude": 1.2, "oil": 1.0, "rupee": 1.1, "dollar": 1.0,
    "nifty": 0.8, "sensex": 0.8, "bank nifty": 0.8, "fii": 1.2, "dii": 1.0,
    "earnings": 1.2, "results": 1.1, "ipo": 0.8, "tariff": 1.3, "war": 1.5,
    "election": 1.3, "q1": 0.7, "q2": 0.7, "q3": 0.7, "q4": 0.7,
}


@dataclass
class Sentiment:
    label: str            # BULLISH | BEARISH | NEUTRAL
    score: float          # net weighted score (magnitude = conviction)
    confidence: int       # 0-100, derived from |score| and impact
    matched: List[str] = field(default_factory=list)  # driving terms
    impact: str = "low"   # low | medium | high

    def to_dict(self) -> Dict[str, Any]:
        return {
            "label": self.label,
            "score": round(self.score, 2),
            "confidence": self.confidence,
            "matched": self.matched[:6],
            "impact": self.impact,
        }


_WORD_RE = re.compile(r"[a-z0-9]+")


def _term_score(text: str, terms: Dict[str, float]) -> Tuple[float, List[str]]:
    score = 0.0
    matched: List[str] = []
    for term, w in terms.items():
        if " " in term or "-" in term:
            if term in text:
                score += w
                matched.append(term)
        else:
            if re.search(rf"\b{re.escape(term)}\b", text):
                score += w
                matched.append(term)
    return score, matched


def classify(title: str) -> Sentiment:
    """Explainable sentiment for a single headline."""
    text = (title or "").lower()

    bull, bull_terms = _term_score(text, BULLISH_TERMS)
    bear, bear_terms = _term_score(text, BEARISH_TERMS)
    net = bull - bear

    # Impact multiplier from market-relevant entities.
    impact_boost = 1.0
    impact_terms = []
    for term, w in HIGH_IMPACT.items():
        if (" " in term and term in text) or re.search(rf"\b{re.escape(term)}\b", text):
            impact_boost += (w - 1.0) * 0.5
            impact_terms.append(term)
    net *= impact_boost

    if net >= 1.0:
        label = "BULLISH"
    elif net <= -1.0:
        label = "BEARISH"
    else:
        label = "NEUTRAL"

    # Confidence: saturating function of |net|.
    confidence = int(min(95, abs(net) * 18 + (10 if impact_terms else 0)))

    if abs(net) >= 3.0 or (impact_terms and abs(net) >= 1.5):
        impact = "high"
    elif abs(net) >= 1.5:
        impact = "medium"
    else:
        impact = "low"

    matched = (bull_terms if net >= 0 else bear_terms) or (bull_terms + bear_terms)
    return Sentiment(
        label=label,
        score=net,
        confidence=confidence,
        matched=matched,
        impact=impact,
    )


def summarize(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate a list of classified {sentiment} items into an overall read."""
    bull = sum(1 for i in items if i.get("sentiment", {}).get("label") == "BULLISH")
    bear = sum(1 for i in items if i.get("sentiment", {}).get("label") == "BEARISH")
    neutral = sum(1 for i in items if i.get("sentiment", {}).get("label") == "NEUTRAL")
    total = max(1, bull + bear + neutral)

    # Weighted net: +1 per bullish, -1 per bearish, scaled by confidence.
    weighted = 0.0
    for i in items:
        s = i.get("sentiment") or {}
        c = s.get("confidence", 0) / 100.0
        if s.get("label") == "BULLISH":
            weighted += c
        elif s.get("label") == "BEARISH":
            weighted -= c
    net = weighted / total

    if net >= 0.15:
        stance = "BULLISH"
    elif net <= -0.15:
        stance = "BEARISH"
    else:
        stance = "NEUTRAL"

    return {
        "stance": stance,
        "netScore": round(net, 3),
        "bullish": bull,
        "bearish": bear,
        "neutral": neutral,
        "total": total,
    }
