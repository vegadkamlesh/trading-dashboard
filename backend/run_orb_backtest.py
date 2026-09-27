r"""ORB research tool: backtest the Opening-Range Breakout on real Dhan 1-min data.

Usage (from backend/):
    .\.venv\Scripts\python.exe run_orb_backtest.py --days 500 --indices NIFTY,SENSEX

Data is fetched once and cached to ./orb_cache_<INDEX>.pkl so re-runs are free
(Dhan's data API is rate-limited). Delete those files to force a re-fetch.

Reports:
  1. how far breakouts run past the OR (MFE in OR-range multiples)
  2. baseline = SL at the opposite OR boundary (your original rule)
  3. a full stop-size x target-size sweep, scored by expectancy in R
  4. best configs with entry-time filters / break-even trailing
  5. combined "one trade/day, first index to break wins"
"""
from __future__ import annotations

import argparse
import asyncio
import os
import pickle
import sys
from datetime import date, timedelta

from dotenv import load_dotenv

load_dotenv()

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:  # pragma: no cover
    pass

from app.dhan_client import get_dhan_client  # noqa: E402
from app.instruments import get_index  # noqa: E402
from app.opening_range import fetch_intraday_raw  # noqa: E402
from app import orb  # noqa: E402

DELTA = 0.5  # assumed ATM option delta (rough per-lot rupee view)
SL_MULTS = (0.2, 0.3, 0.4, 0.5, 0.75, 1.0)
TGT_MULTS = (0.5, 0.75, 1.0, 1.5, 2.0, 3.0)


def _cache_path(k: str) -> str:
    return os.path.join(os.path.dirname(__file__), f"orb_cache_{k}.pkl")


async def _load(index_key: str, cal_days: int, refresh: bool):
    path = _cache_path(index_key)
    if not refresh and os.path.exists(path):
        with open(path, "rb") as fh:
            bars = pickle.load(fh)
        mt = date.fromtimestamp(os.path.getmtime(path))
        print(f"  {index_key}: loaded cache ({mt}) -> {len(bars)} days")
        return bars
    inst = get_index(index_key)
    end = date.today()
    start = end - timedelta(days=cal_days)
    print(f"  {index_key}: fetching {start} -> {end} ...", flush=True)
    raws, err = await fetch_intraday_raw(inst, start, end)
    bars = orb.parse_bars(raws)
    with open(path, "wb") as fh:
        pickle.dump(bars, fh)
    print(f"    -> {len(bars)} trading days" + (f"  (err={err})" if err else ""))
    return bars


def _hdr() -> str:
    return (f"{'stop':>10} {'target':>10} {'trades':>7} {'win%':>6} {'avgW':>7} "
            f"{'avgL':>7} {'R:R':>5} {'expR':>6} {'PF':>5} {'netPts':>8} {'maxDD':>7}")


def _row(cfg: orb.StrategyConfig, s: dict, lot: int) -> str:
    sl = "opposite" if cfg.sl_kind == "opposite" else f"{cfg.sl_value}x OR"
    tg = cfg.target.name
    return (f"{sl:>10} {tg:>10} {s['trades']:>7} {s['winRate']:>6.1f} {s['avgWinPoints']:>7.1f} "
            f"{s['avgLossPoints']:>7.1f} {str(s['rr']):>5} {str(s['expectancyR']):>6} "
            f"{str(s['profitFactor']):>5} {s['netPoints']:>8.0f} {s['maxDrawdownPoints']:>7.0f}")


def _sweep(bars: dict, k: str, lot: int, sl_kind: str, sl_values, tgt_values) -> list:
    rows = []
    for sl in sl_values:
        for tg in tgt_values:
            cfg = orb.StrategyConfig(
                target=orb.TargetRule(f"{tg}x", "range_mult", tg),
                sl_kind=sl_kind, sl_value=sl,
            )
            trades = orb.collect_trades(bars, cfg, k)
            s = orb.summarize(trades, lot, DELTA)
            if s.get("trades"):
                rows.append((sl, tg, s))
    return rows


def _grid(rows: list, tgt_values) -> None:
    """Compact expectancy-in-R heat grid: rows = stop, cols = target."""
    print("\n  Expectancy in R (avg profit per trade / risk).  '+' = profitable")
    print("        " + "".join(f"{t:>9.2f}x" for t in tgt_values) + "   <- target")
    stops = sorted({r[0] for r in rows})
    for sl in stops:
        cells = []
        for t in tgt_values:
            m = [r for r in rows if r[0] == sl and r[1] == t]
            cells.append(f"{m[0][2]['expectancyR']:>10.2f}" if m else f"{'-':>10}")
        best = max((r for r in rows if r[0] == sl), key=lambda r: r[2]["expectancyR"], default=None)
        flag = "  <== best stop" if best and best[2]["expectancyR"] > 0 else ""
        print(f"  {sl:>4}x " + "".join(cells) + f"   stop  {flag}")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=500)
    ap.add_argument("--indices", default="NIFTY,SENSEX")
    ap.add_argument("--refresh", action="store_true", help="ignore cache, re-fetch")
    args = ap.parse_args()

    cal_days = int(args.days * 1.5) + 60
    keys = [k.strip().upper() for k in args.indices.split(",") if k.strip()]

    print("=" * 96)
    print(f"ORB BACKTEST  |  {args.days} trading days  |  1 lot  |  delta={DELTA}")
    print("=" * 96)

    bars_by_index = {}
    for k in keys:
        bars_by_index[k] = await _load(k, cal_days, args.refresh)

    for k in keys:
        inst = get_index(k)
        lot = inst.lot_size
        allbars = bars_by_index[k]
        days = sorted(allbars)[-args.days:]
        bars = {d: allbars[d] for d in days}

        print("\n" + "=" * 96)
        print(f"### {k}   lot={lot}   {days[0]} -> {days[-1]}   ({len(days)} days)")
        print("=" * 96)

        ref = orb.StrategyConfig(target=orb.TargetRule("ref", "range_mult", 50.0))
        base_trades = orb.collect_trades(bars, ref, k)
        dist = orb.mfe_distribution(base_trades)
        avg_or = sum(t.or_range for t in base_trades) / max(len(base_trades), 1)
        print(f"\nBreakout days: {len(base_trades)}/{len(days)}   avg OR range: {avg_or:.1f} pts")
        print("How far spot runs PAST the breakout (x OR-range)  [MFE]:")
        for p in ("p30", "p50", "p60", "p70", "p80", "p90"):
            v = dist.get(p, 0)
            print(f"   {p}: {v:>5.2f}x   {'#' * int(round(v * 8))}")
        print(f"   mean {dist.get('mean')}x   median {dist.get('median')}x")

        # ---- 2. baseline: your original rule (SL = opposite OR boundary) ----
        print("\n--- BASELINE: SL = opposite OR boundary (risk = full OR range) ---")
        print(_hdr())
        print("-" * len(_hdr()))
        b_rows = []
        for tg in (0.3, 0.5, 0.75, 1.0, 1.25, 1.5):
            cfg = orb.StrategyConfig(target=orb.TargetRule(f"{tg}x", "range_mult", tg))
            s = orb.summarize(orb.collect_trades(bars, cfg, k), lot, DELTA)
            if s.get("trades"):
                b_rows.append((1.0, tg, s))
                print(_row(cfg, s, lot))

        # ---- 3. stop-size x target-size sweep ----
        print("\n--- SWEEP: tighter stop x target (SL = k x OR-range) ---")
        print(_hdr())
        print("-" * len(_hdr()))
        rows = _sweep(bars, k, lot, "range_mult", SL_MULTS, TGT_MULTS)
        # print only profitable-ish rows to keep it readable
        for sl, tg, s in sorted(rows, key=lambda r: -r[2]["expectancyR"])[:12]:
            cfg = orb.StrategyConfig(target=orb.TargetRule(f"{tg}x", "range_mult", tg),
                                     sl_kind="range_mult", sl_value=sl)
            print(_row(cfg, s, lot))
        _grid(rows, TGT_MULTS)

        # ---- 4. best config + refinements ----
        best = max(rows, key=lambda r: r[2]["expectancyR"])
        bsl, btg, bs = best
        print(f"\n--- BEST: SL {bsl}x OR, target {btg}x OR  (expR {bs['expectancyR']}) ---")
        refines = [
            ("+ entry before 10:30", orb.StrategyConfig(
                target=orb.TargetRule(f"{btg}x", "range_mult", btg), sl_kind="range_mult",
                sl_value=bsl, max_entry_min=10 * 60 + 30)),
            ("+ entry before 11:00", orb.StrategyConfig(
                target=orb.TargetRule(f"{btg}x", "range_mult", btg), sl_kind="range_mult",
                sl_value=bsl, max_entry_min=11 * 60)),
            ("+ break-even at 1x OR", orb.StrategyConfig(
                target=orb.TargetRule(f"{btg}x", "range_mult", btg), sl_kind="range_mult",
                sl_value=bsl, breakeven_at=1.0)),
            ("+ OR-size filter 0.1-0.6%", orb.StrategyConfig(
                target=orb.TargetRule(f"{btg}x", "range_mult", btg), sl_kind="range_mult",
                sl_value=bsl, min_or_pct=0.1, max_or_pct=0.6)),
        ]
        print(_hdr())
        print("-" * len(_hdr()))
        for label, cfg in refines:
            s = orb.summarize(orb.collect_trades(bars, cfg, k), lot, DELTA)
            if s.get("trades"):
                print(_row(cfg, s, lot) + f"   {label}")

        # ---- 4b. cost / slippage sensitivity on the best config ----
        best_cfg = orb.StrategyConfig(
            target=orb.TargetRule(f"{btg}x", "range_mult", btg),
            sl_kind="range_mult", sl_value=bsl,
        )
        raw = orb.collect_trades(bars, best_cfg, k)
        print(f"\n--- COST SENSITIVITY (best cfg: SL {bsl}x, target {btg}x) ---")
        print(f"{'round-trip cost':>16} {'netPts':>9} {'PF':>6} {'netRs/lot':>11} {'maxDD Rs':>10} {'verdict':>12}")
        for rupees in (0, 50, 100, 150, 200, 300, 400):
            cost_pts = rupees / (DELTA * lot)
            s = orb.summarize(orb.apply_cost(raw, cost_pts), lot, DELTA)
            verdict = "PROFIT" if s["netPoints"] > 0 else "loss"
            print(f"{('Rs ' + str(rupees)):>16} {s['netPoints']:>9.0f} {str(s['profitFactor']):>6} "
                  f"{s['netRupeesPerLot']:>11.0f} {s['maxDrawdownRupeesPerLot']:>10.0f} {verdict:>12}")

    # ---- 5. combined one-trade-per-day ----
    if len(keys) >= 2:
        print("\n" + "=" * 96)
        print("### COMBINED - ONE trade/day (the index that breaks out FIRST wins)")
        print("=" * 96)
        barsets = {}
        for k in keys:
            ab = bars_by_index[k]
            ds = sorted(ab)[-args.days:]
            barsets[k] = {d: ab[d] for d in ds}
        common = sorted(set.intersection(*[set(b) for b in barsets.values()]))
        print(f"common trading days: {len(common)}")
        print(_hdr())
        print("-" * len(_hdr()))
        for sl in (0.2, 0.3, 0.4, 0.5):
            for tg in (1.0, 1.5, 2.0):
                per = {}
                for k in keys:
                    cfg = orb.StrategyConfig(
                        target=orb.TargetRule(f"{tg}x", "range_mult", tg),
                        sl_kind="range_mult", sl_value=sl)
                    per[k] = orb.collect_trades({d: barsets[k][d] for d in common}, cfg, k)
                combo = orb.combined_one_trade_per_day(per)
                s = orb.summarize(combo, 1, 1.0)  # points-only view
                if s.get("trades"):
                    lots = {k: get_index(k).lot_size for k in keys}
                    net_rs, dd_rs = orb.net_rupees(combo, lots, DELTA)
                    net_rs5, _ = orb.net_rupees(orb.apply_cost(combo, 150 / (DELTA * 65)), lots, DELTA)
                    cfg_lbl = orb.StrategyConfig(
                        target=orb.TargetRule(f"{tg}x", "range_mult", tg),
                        sl_kind="range_mult", sl_value=sl)
                    print(_row(cfg_lbl, s, 1)
                          + f"   netRs(1lot each) {net_rs:>8.0f}  maxDD {dd_rs:>7.0f}  "
                          + f"@Rs150/trd {net_rs5:>8.0f}")

    await get_dhan_client().aclose()
    print("\ndone.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
