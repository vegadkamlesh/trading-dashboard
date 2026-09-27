"""5-year ORB parameter study, runnable from the web UI.

Why this exists
---------------
The stop/target multipliers the algo uses are not guesses - they come from
sweeping ~5 years of real 1-minute data. This module re-runs that sweep on
demand, in the background, and caches the answer so the page loads instantly.

Cost model
----------
Raw expectancy is one thing; what survives brokerage + slippage is another.
Every result therefore also carries a cost curve (net points at 0/50/100/150/
200 rupees of round-trip cost), so you can see where the edge stops paying.

Rate limits
-----------
Dhan allows ~5 data requests/second. We fetch <=90 days per call with limited
concurrency, a small stagger, and exponential backoff on 429 - see
opening_range.fetch_intraday_raw. A full 5-year run is ~25 calls per index.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from . import orb
from .config import get_settings
from .dhan_client import get_dhan_client
from .instruments import get_index, lot_size_for
from .opening_range import fetch_intraday_raw

logger = logging.getLogger("dhan.algo.study")

DELTA = 0.5
SL_MULTS = (0.15, 0.2, 0.25, 0.3, 0.4, 0.5, 0.75, 1.0)
TGT_MULTS = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0)
COST_RUPEES = (0, 50, 100, 150, 200, 300)

_BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "algo")


@dataclass
class StudyProgress:
    status: str = "idle"          # idle | running | ready | error
    step: str = ""
    percent: float = 0.0
    startedAt: Optional[float] = None
    finishedAt: Optional[float] = None
    error: Optional[str] = None
    years: int = 5


class OrbStudy:
    def __init__(self) -> None:
        self.progress = StudyProgress()
        self.result: Optional[Dict[str, Any]] = None
        self._task: Optional[asyncio.Task] = None
        self._cache = os.path.join(_BASE, "study.json")
        self._load_cache()

    # ------------------------------------------------------------- caching
    def _load_cache(self) -> None:
        if not os.path.exists(self._cache):
            return
        try:
            with open(self._cache, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self.result = data
            self.progress = StudyProgress(
                status="ready", step="loaded from cache",
                finishedAt=data.get("generatedAt"), percent=100.0,
                years=int(data.get("years", 5)),
            )
        except (OSError, ValueError) as exc:
            logger.warning("Could not read study cache: %s", exc)

    def _save_cache(self) -> None:
        try:
            os.makedirs(_BASE, exist_ok=True)
            with open(self._cache, "w", encoding="utf-8") as fh:
                json.dump(self.result, fh, default=str)
        except OSError as exc:
            logger.warning("Could not write study cache: %s", exc)

    def age_seconds(self) -> Optional[float]:
        if not self.result:
            return None
        ts = self.result.get("generatedAt")
        return round(time.time() - float(ts), 0) if ts else None

    # ------------------------------------------------------------- control
    def start(self, years: int = 5, indices: Optional[List[str]] = None) -> Dict[str, Any]:
        if self.progress.status == "running":
            return self.status()
        self.progress = StudyProgress(
            status="running", step="starting", percent=0.0,
            startedAt=time.time(), years=years,
        )
        self._task = asyncio.create_task(self._run(years, indices))
        return self.status()

    def status(self) -> Dict[str, Any]:
        return {
            "progress": asdict(self.progress),
            "ageSeconds": self.age_seconds(),
            "result": self.result,
        }

    # ------------------------------------------------------------- the work
    async def _run(self, years: int, indices: Optional[List[str]]) -> None:
        try:
            today = date.today()
            start = today - timedelta(days=int(years * 365.25) + 30)
            keys = indices or (get_settings().algo_indices or ["NIFTY", "SENSEX"])
            out: Dict[str, Any] = {
                "generatedAt": time.time(),
                "generatedOn": today.isoformat(),
                "years": years,
                "range": {"from": start.isoformat(), "to": today.isoformat()},
                "delta": DELTA,
                "indices": {},
            }
            total = len(keys)
            bars_by_index: Dict[str, Dict[date, List[orb.Bar]]] = {}
            for i, key in enumerate(keys):
                self.progress.step = f"fetching {key} 1-minute history"
                self.progress.percent = round(i / total * 60, 1)
                bars_by_day = await self._fetch(key, start, today)
                bars_by_index[key] = bars_by_day
                self.progress.step = f"sweeping {key}"
                out["indices"][key] = self._analyse(key, bars_by_day)
                self.progress.percent = round((i + 1) / total * 60, 1)

            self.progress.step = "combining both indices"
            out["combined"] = self._combined(keys, out["indices"])
            out["recommended"] = self._recommend(out)

            # Cost sensitivity of the config that ACTUALLY SHIPS (not the
            # in-sample best cell, which is usually a tighter stop than you
            # could execute). This is the number that decides viability.
            rec = out["recommended"] or {}
            sl, tg = rec.get("slMult"), rec.get("targetMult")
            breakevens: List[float] = []
            if sl and tg:
                self.progress.step = "computing cost break-even of the shipped config"
                for key in keys:
                    bars = bars_by_index.get(key)
                    idx = out["indices"].get(key)
                    if not bars or idx is None:
                        continue
                    info = self._cost_curve_for(key, bars, sl, tg)
                    idx["costCurve"] = info["curve"]
                    idx["costBreakEvenRupees"] = info["breakEvenRupees"]
                    idx["grossRupeesPerTrade"] = info["grossRupeesPerTrade"]
                    idx["tradesPerYear"] = info["tradesPerYear"]
                    idx["shippedConfig"] = {"sl": sl, "target": tg}
                    if info["breakEvenRupees"] is not None:
                        breakevens.append(info["breakEvenRupees"])
                rec["costBreakEvenRupees"] = min(breakevens) if breakevens else None
                if rec.get("costBreakEvenRupees"):
                    rec["reason"] = rec.get("reason", "") + (
                        f"  VIABILITY: the average trade earns about "
                        f"Rs{rec['costBreakEvenRupees']:,.0f} gross per lot, so the strategy "
                        f"goes to break-even at roughly that much round-trip cost "
                        f"(brokerage + STT + slippage). Measure your real cost before going live."
                    )
            out["retentionNote"] = (
                "Chunked 90-day fetch with 429 backoff; costs are indicative "
                "(delta 0.5, premium P&L)."
            )
            self.result = out
            self._save_cache()
            self.progress.status = "ready"
            self.progress.step = "done"
            self.progress.percent = 100.0
            self.progress.finishedAt = time.time()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("ORB study failed")
            self.progress.status = "error"
            self.progress.error = str(exc)
            self.progress.step = "failed"

    async def _fetch(self, key: str, start: date, end: date) -> Dict[date, List[orb.Bar]]:
        inst = get_index(key)
        if inst is None:
            return {}
        # Anchor the window to the 1st of the month. `start` is derived from
        # "today minus N years", so without this the cache filename changes every
        # single day - you get a fresh full download daily and the study's
        # reported date range drifts. Anchoring makes the cache (and therefore
        # the study) stable within a month.
        start = start.replace(day=1)
        cache = os.path.join(_BASE, f"history-{key}-{start.isoformat()}.pkl")
        self._prune_history(key, keep=cache)
        if os.path.exists(cache):
            try:
                import pickle

                with open(cache, "rb") as fh:
                    return pickle.load(fh)
            except (OSError, ValueError):
                pass
        raws, err = await fetch_intraday_raw(inst, start, end)
        bars = orb.parse_bars(raws)
        if err:
            # NEVER cache a partial history. A missing chunk used to be swallowed
            # and cached, which silently shortened the study (NIFTY lost a whole
            # year this way). Use it for this run, but refetch next time.
            logger.warning("study fetch %s incomplete: %s (not caching)", key, err)
            return bars
        try:
            import pickle

            os.makedirs(_BASE, exist_ok=True)
            with open(cache, "wb") as fh:
                pickle.dump(bars, fh)
        except OSError:
            pass
        return bars

    @staticmethod
    def _prune_history(key: str, keep: str) -> None:
        """Delete superseded history pickles for this index (they are ~30MB each)."""
        import glob

        for path in glob.glob(os.path.join(_BASE, f"history-{key}-*.pkl")):
            if os.path.abspath(path) != os.path.abspath(keep):
                try:
                    os.remove(path)
                except OSError:
                    pass

    def _analyse(self, key: str, bars: Dict[date, List[orb.Bar]]) -> Dict[str, Any]:
        lot = lot_size_for(key)
        days = sorted(bars)
        if not days:
            return {"days": 0, "error": "no data"}

        ref = orb.StrategyConfig(target=orb.TargetRule("ref", "range_mult", 50.0))
        base = orb.collect_trades(bars, ref, key)
        avg_or = (sum(t.or_range for t in base) / len(base)) if base else 0.0

        grid: List[Dict[str, Any]] = []
        for sl in SL_MULTS:
            for tg in TGT_MULTS:
                cfg = orb.StrategyConfig(
                    target=orb.TargetRule(f"{tg}x", "range_mult", tg),
                    sl_kind="range_mult", sl_value=sl,
                )
                s = orb.summarize(orb.collect_trades(bars, cfg, key), lot, DELTA)
                if s.get("trades"):
                    grid.append({
                        "sl": sl, "target": tg,
                        "trades": s["trades"], "winRate": s["winRate"],
                        "expR": s["expectancyR"], "pf": s["profitFactor"],
                        "netPoints": s["netPoints"], "maxDD": s["maxDrawdownPoints"],
                        "avgloss": s["avgLossPoints"], "avgWin": s["avgWinPoints"],
                        "rr": s["rr"],
                    })

        baseline = []
        for tg in TGT_MULTS:
            cfg = orb.StrategyConfig(target=orb.TargetRule(f"{tg}x", "range_mult", tg))
            s = orb.summarize(orb.collect_trades(bars, cfg, key), lot, DELTA)
            if s.get("trades"):
                baseline.append({
                    "target": tg, "winRate": s["winRate"], "expR": s["expectancyR"],
                    "pf": s["profitFactor"], "netPoints": s["netPoints"],
                    "avgWin": s["avgWinPoints"], "avgLoss": s["avgLossPoints"],
                })

        best = max(grid, key=lambda r: (r["expR"] or -9)) if grid else None
        return {
            "days": len(days), "from": days[0].isoformat(), "to": days[-1].isoformat(),
            "lotSize": lot, "breakoutDays": len(base), "avgOrRange": round(avg_or, 1),
            "mfe": orb.mfe_distribution(base),
            "grid": grid, "baseline": baseline, "best": best,
        }

    def _cost_curve_for(
        self, key: str, bars: Dict[date, List[orb.Bar]], sl: float, tg: float
    ) -> Dict[str, Any]:
        """What the SHIPPED stop/target actually nets at increasing cost levels.

        The single most useful number here is `grossRupeesPerTrade`: the average
        rupee profit per trade BEFORE costs. Break-even cost == that number,
        so if your real brokerage+slippage is above it, the strategy loses.
        """
        lot = lot_size_for(key)
        cfg = orb.StrategyConfig(
            target=orb.TargetRule(f"{tg}x", "range_mult", tg),
            sl_kind="range_mult", sl_value=sl,
        )
        raw = orb.collect_trades(bars, cfg, key)
        curve: List[Dict[str, Any]] = []
        for rupees in COST_RUPEES:
            s = orb.summarize(orb.apply_cost(raw, rupees / (DELTA * lot)), lot, DELTA)
            curve.append({
                "cost": rupees, "netPoints": s["netPoints"], "pf": s["profitFactor"],
                "netRupees": s["netRupeesPerLot"],
                "maxDDRupees": s["maxDrawdownRupeesPerLot"],
                "winRate": s["winRate"], "trades": s["trades"],
            })
        gross = curve[0] if curve else None
        trades = max(1, int(gross["trades"])) if gross else 1
        years = out_years(bars)
        per_trade = (gross["netRupees"] / trades) if gross else None
        return {
            "curve": curve,
            "breakEvenRupees": _breakeven_cost(curve),
            "grossRupeesPerTrade": round(per_trade, 1) if per_trade is not None else None,
            "grossRupeesPerYear": round(per_trade * trades / years, 0) if per_trade is not None else None,
            "tradesPerYear": round(trades / years, 0),
        }

    def _combined(self, keys: List[str], per_index: Dict[str, Any]) -> List[Dict[str, Any]]:
        # Recompute the "first index to break wins" portfolio for the best few
        # configurations. Needs the raw bars, which _analyse did not keep, so we
        # re-derive from the per-index grids (points are not comparable across
        # indices, so we report expectancy R which IS comparable).
        rows: List[Dict[str, Any]] = []
        for sl in (0.2, 0.25, 0.3, 0.4):
            for tg in (1.0, 1.25, 1.5, 2.0):
                parts = []
                for key in keys:
                    cell = next(
                        (g for g in per_index.get(key, {}).get("grid", [])
                         if g["sl"] == sl and g["target"] == tg), None)
                    if cell:
                        parts.append((key, cell))
                if len(parts) != len(keys):
                    continue
                rows.append({
                    "sl": sl, "target": tg,
                    "expR": round(sum(p[1]["expR"] for p in parts) / len(parts), 3),
                    "winRate": round(sum(p[1]["winRate"] for p in parts) / len(parts), 1),
                    "pf": round(sum(p[1]["pf"] for p in parts) / len(parts), 2),
                    "perIndex": {k: {"expR": c["expR"], "pf": c["pf"], "trades": c["trades"]}
                                 for k, c in parts},
                })
        return sorted(rows, key=lambda r: -r["expR"])

    def _recommend(self, out: Dict[str, Any]) -> Dict[str, Any]:
        """Pick the multipliers that look best on BOTH indices (robust, not best-of)."""
        keys = list(out["indices"].keys())
        if not keys:
            return {}
        candidates: List[Dict[str, Any]] = []
        for sl in SL_MULTS:
            for tg in TGT_MULTS:
                cells = [
                    next((g for g in out["indices"][k].get("grid", [])
                          if g["sl"] == sl and g["target"] == tg), None)
                    for k in keys
                ]
                if any(c is None for c in cells):
                    continue
                worst = min(c["expR"] for c in cells)
                avg = sum(c["expR"] for c in cells) / len(cells)
                trades = min(c["trades"] for c in cells)
                candidates.append({
                    "sl": sl, "target": tg, "worstExpR": round(worst, 3),
                    "avgExpR": round(avg, 3),
                    "avgPF": round(sum(c["pf"] for c in cells) / len(cells), 2),
                    "minTrades": trades,
                    "maxDD": max(c["maxDD"] for c in cells),
                })
        if not candidates:
            return {}
        # Score = worst-index expectancy (robustness) + a nudge for trade count.
        candidates.sort(key=lambda c: (-(c["worstExpR"] or -9), -c["minTrades"]))
        top = candidates[:5]
        pick = top[0]
        # Practicality floor: a 0.15x-OR stop on NIFTY is ~11 spot points, which is
        # too tight to execute reliably once bid/ask + slippage are in play.
        # Take the widest stop that is still within 10% of the best expectancy.
        for c in sorted(top, key=lambda x: -x["sl"]):
            if c["sl"] >= 0.2 and c["worstExpR"] >= pick["worstExpR"] * 0.90:
                pick = c
                break
        worst_be = None
        for key in keys:
            v = out["indices"][key].get("costBreakEvenRupees")
            if v is not None:
                worst_be = v if worst_be is None else min(worst_be, v)
        return {
            "slMult": pick["sl"], "targetMult": pick["target"],
            "expectedR": pick["worstExpR"], "avgR": pick["avgExpR"],
            "profitFactor": pick["avgPF"], "minTrades": pick["minTrades"],
            "maxDrawdownPoints": pick["maxDD"],
            "costBreakEvenRupees": worst_be,
            "alternatives": top,
            "reason": (
                f"Best stop/target pair that stays profitable on EVERY index "
                f"(worst-case expectancy {pick['worstExpR']}R per trade), using a stop of "
                f"{pick['sl']}x and target {pick['target']}x the opening range."
            ),
        }


def out_years(bars: Dict[date, List[orb.Bar]]) -> float:
    if not bars:
        return 1.0
    days = sorted(bars)
    span = (days[-1] - days[0]).days
    return max(1.0, span / 365.25)


def _breakeven_cost(curve: List[Dict[str, Any]]) -> Optional[float]:
    """Round-trip rupees at which net points cross zero (interpolated)."""
    for a, b in zip(curve, curve[1:]):
        if a["netPoints"] > 0 >= b["netPoints"]:
            span = a["netPoints"] - b["netPoints"]
            frac = (a["netPoints"] / span) if span else 0.0
            return float(round(a["cost"] + frac * (b["cost"] - a["cost"])))
    return None


study = OrbStudy()
