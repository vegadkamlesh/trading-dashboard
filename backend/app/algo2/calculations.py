"""Pure, side-effect-free maths for Algo 2.

Inputs are the option-chain rows produced by ``optionchain._transform_chain``::

    {"strike": 23500.0,
     "ce": {"oi": .., "greeks": {"delta": .., "gamma": ..}, ...},
     "pe": {...}}

and 5-minute candles ``{"t": epoch, "open", "high", "low", "close", "volume"}``.
Plain Python on purpose: a chain has ~100 rows and a day ~75 candles, so this is
well under a millisecond and keeps the server free of numpy/pandas.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


def _num(value: Any) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    return out if math.isfinite(out) else 0.0


def _leg(row: Mapping[str, Any], side: str) -> Mapping[str, Any]:
    return row.get(side) or {}


def _greek(row: Mapping[str, Any], side: str, name: str) -> float:
    return _num((_leg(row, side).get("greeks") or {}).get(name))


def near_rows(rows: Sequence[Mapping[str, Any]], spot: float, strikes_each_side: int) -> List[Mapping[str, Any]]:
    """ATM +/- N strikes (all rows when N <= 0 or spot unknown)."""
    if not rows or strikes_each_side <= 0 or not spot:
        return list(rows)
    ordered = sorted(rows, key=lambda r: _num(r.get("strike")))
    atm = min(range(len(ordered)), key=lambda i: abs(_num(ordered[i].get("strike")) - spot))
    return ordered[max(0, atm - strikes_each_side): atm + strikes_each_side + 1]


def compute_net_delta(rows: Iterable[Mapping[str, Any]]) -> float:
    """Sum(PE_OI * |PE_delta|) - Sum(CE_OI * CE_delta)."""
    total = 0.0
    for row in rows:
        total += _num(_leg(row, "pe").get("oi")) * abs(_greek(row, "pe", "delta"))
        total -= _num(_leg(row, "ce").get("oi")) * _greek(row, "ce", "delta")
    return total


def compute_net_gex(rows: Iterable[Mapping[str, Any]]) -> float:
    """Sum(PE_OI * PE_gamma) - Sum(CE_OI * CE_gamma). Negative = gamma-squeeze/expansion zone."""
    total = 0.0
    for row in rows:
        total += _num(_leg(row, "pe").get("oi")) * _greek(row, "pe", "gamma")
        total -= _num(_leg(row, "ce").get("oi")) * _greek(row, "ce", "gamma")
    return total


def oi_totals(rows: Iterable[Mapping[str, Any]]) -> Tuple[float, float]:
    """(total CE OI, total PE OI)."""
    ce = pe = 0.0
    for row in rows:
        ce += _num(_leg(row, "ce").get("oi"))
        pe += _num(_leg(row, "pe").get("oi"))
    return ce, pe


def compute_pcr(rows: Iterable[Mapping[str, Any]]) -> Optional[float]:
    ce, pe = oi_totals(rows)
    return pe / ce if ce > 0 else None


def compute_oi_velocity(current: Mapping[str, float], previous: Mapping[str, float]) -> Dict[str, Optional[float]]:
    """% change of CE / PE OI versus the sample ~5 minutes earlier.

    ``current`` / ``previous`` are ``{"ce": total_ce_oi, "pe": total_pe_oi}``.
    """
    out: Dict[str, Optional[float]] = {}
    for side in ("ce", "pe"):
        before = _num(previous.get(side))
        now = _num(current.get(side))
        out[side] = (now - before) / before * 100.0 if before > 0 else None
    return out


def oi_velocity_spike(velocity: Mapping[str, Optional[float]], side: str, threshold: float) -> bool:
    value = velocity.get(side)
    return value is not None and value > threshold


def compute_hsmi(changes_pct: Mapping[str, Optional[float]], weights: Mapping[str, float]) -> Optional[float]:
    """Heavyweight Stock Momentum Index: weight-normalised average % change.

    Positive = heavyweights are lifting the index. Stocks without a quote are
    skipped; None when no weighted stock has a quote.
    """
    num = den = 0.0
    for symbol, weight in weights.items():
        change = changes_pct.get(symbol)
        if change is None or weight <= 0:
            continue
        num += weight * change
        den += weight
    return num / den if den > 0 else None


def compute_vwap_bands(candles: Sequence[Mapping[str, Any]], band_sd: float = 1.5) -> Optional[Dict[str, float]]:
    """Session-anchored VWAP of typical price with +/- band_sd volume-weighted std-dev bands."""
    vol_sum = pv_sum = 0.0
    for c in candles:
        vol = _num(c.get("volume"))
        if vol <= 0:
            continue
        tp = (_num(c.get("high")) + _num(c.get("low")) + _num(c.get("close"))) / 3.0
        vol_sum += vol
        pv_sum += tp * vol
    if vol_sum <= 0:
        return None
    vwap = pv_sum / vol_sum
    var = 0.0
    for c in candles:
        vol = _num(c.get("volume"))
        if vol <= 0:
            continue
        tp = (_num(c.get("high")) + _num(c.get("low")) + _num(c.get("close"))) / 3.0
        var += vol * (tp - vwap) ** 2
    sd = math.sqrt(var / vol_sum)
    return {"vwap": vwap, "sd": sd, "upper": vwap + band_sd * sd, "lower": vwap - band_sd * sd}


def compute_market_profile(
    candles: Sequence[Mapping[str, Any]], bin_size: float, value_area: float = 0.70,
) -> Optional[Dict[str, float]]:
    """Volume profile: Point of Control and the 70% value area (VAH / VAL).

    Each candle's volume is spread evenly over the price bins its range touches.
    """
    if bin_size <= 0:
        return None
    bins: Dict[int, float] = {}
    for c in candles:
        vol = _num(c.get("volume"))
        lo, hi = _num(c.get("low")), _num(c.get("high"))
        if vol <= 0 or lo <= 0 or hi < lo:
            continue
        first, last = int(math.floor(lo / bin_size)), int(math.floor(hi / bin_size))
        share = vol / (last - first + 1)
        for b in range(first, last + 1):
            bins[b] = bins.get(b, 0.0) + share
    if not bins:
        return None
    total = sum(bins.values())
    lowest, highest = min(bins), max(bins)
    poc = max(bins, key=lambda b: (bins[b], -b))
    lo_b = hi_b = poc
    covered = bins[poc]
    while covered < value_area * total and (lo_b > lowest or hi_b < highest):
        below = bins.get(lo_b - 1, 0.0) if lo_b > lowest else -1.0
        above = bins.get(hi_b + 1, 0.0) if hi_b < highest else -1.0
        if above >= below:
            hi_b += 1
            covered += above
        else:
            lo_b -= 1
            covered += below
    return {"poc": poc * bin_size + bin_size / 2.0, "vah": (hi_b + 1) * bin_size, "val": lo_b * bin_size}


def compute_basis(spot: Optional[float], futures: Optional[float]) -> Optional[float]:
    """Futures - spot (negative = discount)."""
    if not spot or not futures:
        return None
    return futures - spot


def basis_divergence(spot_new_high: bool, basis: Optional[float]) -> bool:
    """Bearish divergence flag: spot prints a new high while futures trade at a discount."""
    return bool(spot_new_high and basis is not None and basis < 0)


def oi_wall(rows: Sequence[Mapping[str, Any]], spot: float, side: str, max_strikes: int) -> Optional[float]:
    """Strike with the highest OI beyond spot: CE wall above (CALL target), PE wall below (PUT target)."""
    leg = "ce" if side == "CALL" else "pe"
    candidates = [r for r in rows
                  if (_num(r.get("strike")) > spot if side == "CALL" else 0 < _num(r.get("strike")) < spot)]
    candidates.sort(key=lambda r: abs(_num(r.get("strike")) - spot))
    if max_strikes > 0:
        candidates = candidates[:max_strikes]
    best = max(candidates, key=lambda r: _num(_leg(r, leg).get("oi")), default=None)
    if best is None or _num(_leg(best, leg).get("oi")) <= 0:
        return None
    return _num(best.get("strike"))


def pick_delta_strike(
    rows: Sequence[Mapping[str, Any]], spot: float, side: str, target_delta: float,
) -> Optional[Mapping[str, Any]]:
    """ITM row whose |delta| is closest to ``target_delta`` (CALL: strike < spot, PUT: strike > spot)."""
    leg = "ce" if side == "CALL" else "pe"
    best: Optional[Mapping[str, Any]] = None
    best_gap = float("inf")
    for row in rows:
        strike = _num(row.get("strike"))
        if (side == "CALL" and strike >= spot) or (side == "PUT" and strike <= spot):
            continue
        node = _leg(row, leg)
        delta = abs(_greek(row, leg, "delta"))
        if not node.get("securityId") or delta <= 0:
            continue
        gap = abs(delta - target_delta)
        if gap < best_gap:
            best, best_gap = row, gap
    return best
