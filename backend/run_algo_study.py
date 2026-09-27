"""Run the 5-year ORB study straight from the command line.

This is the SAME code path the web UI uses (app/orb_study.py), so whatever you
see in the Algo tab is exactly what you get here.

    python run_algo_study.py --years 5
    python run_algo_study.py --years 5 --indices NIFTY,SENSEX

Results are cached to backend/data/algo/study.json, so the web page opens
instantly afterwards. Fetched 1-minute history is cached per index too, so a
second run is nearly free.
"""
from __future__ import annotations

import argparse
import asyncio

from app.config import get_settings
from app.dhan_client import close_dhan_client, get_dhan_client
from app.orb_study import study


async def run(years: int, indices: list[str] | None) -> None:
    s = get_settings()
    if not s.is_configured():
        print("ERROR: DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN missing in backend/.env")
        return
    try:
        prof = await get_dhan_client().get_profile()
        print(f"Authenticated as {prof.get('dhanClientName')} ({prof.get('dhanClientId')})")
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: Dhan auth failed: {exc}")
        return

    study.start(years=years, indices=indices)
    last = ""
    while study.progress.status == "running":
        line = f"[{study.progress.percent:5.1f}%] {study.progress.step}"
        if line != last:
            print(line, flush=True)
            last = line
        await asyncio.sleep(1.0)

    if study.progress.status == "error":
        print(f"FAILED: {study.progress.error}")
        return

    res = study.result or {}
    rec = res.get("recommended", {})
    rng = res.get("range", {})
    print("\n" + "=" * 74)
    print(f"5-YEAR ORB STUDY  {rng.get('from')} -> {rng.get('to')}")
    print("=" * 74)
    for key, idx in (res.get("indices") or {}).items():
        if idx.get("error"):
            print(f"\n{key}: ERROR {idx['error']}")
            continue
        print(f"\n{key}  ({idx['days']} sessions, avg OR {idx['avgOrRange']} pts, lot {idx['lotSize']})")
        m = idx.get("mfe", {})
        print(f"  MFE after breakout (x OR): p30 {m.get('p30')}  p50 {m.get('p50')}  "
              f"p70 {m.get('p70')}  p80 {m.get('p80')}  p90 {m.get('p90')}")
        print("  best by expectancy:")
        for r in sorted(idx.get("grid", []), key=lambda x: -x["expR"])[:5]:
            print(f"    stop {r['sl']:>4}x  target {r['target']:>4}x  "
                  f"expR {r['expR']:>6}  PF {r['pf']:>5}  win {r['winRate']:>5}%  "
                  f"n {r['trades']:>5}  net {r['netPoints']:>8} pts")
        print("  cost sensitivity of the SHIPPED config (1 lot):")
        if idx.get("grossRupeesPerTrade") is not None:
            print(f"    average trade grosses Rs{idx['grossRupeesPerTrade']:,.0f} "
                  f"({idx.get('tradesPerYear', 0):,.0f} trades/yr)")
            print(f"    -> breaks even at about Rs{idx.get('costBreakEvenRupees', 0):,.0f} "
                  f"round-trip cost per lot")
        for c in idx.get("costCurve", []):
            print(f"    Rs{c['cost']:>4}/round-trip -> net {c['netPoints']:>8.0f} pts  "
                  f"PF {c['pf']:>5}  Rs{c['netRupees']:>10,.0f}")

    print("\n" + "-" * 74)
    print("RECOMMENDED (profitable on EVERY index):")
    print(f"  stop   = {rec.get('slMult')} x opening range")
    print(f"  target = {rec.get('targetMult')} x opening range")
    print(f"  worst-index expectancy {rec.get('expectedR')}R, avg {rec.get('avgR')}R, "
          f"PF {rec.get('profitFactor')}, maxDD {rec.get('maxDrawdownPoints')} pts")
    if rec.get("costBreakEvenRupees"):
        print(f"  COST BREAK-EVEN: ~Rs{rec['costBreakEvenRupees']:,.0f} round-trip per lot")
    print(f"  {rec.get('reason')}")
    print("-" * 74)
    age = study.age_seconds()
    print(f"\nCached to backend/data/algo/study.json ({age:.0f}s ago)" if age else "")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=5)
    ap.add_argument("--indices", type=str, default="")
    args = ap.parse_args()
    idx = [s.strip().upper() for s in args.indices.split(",") if s.strip()] or None
    try:
        asyncio.run(_wrapped(run(args.years, idx)))
    except KeyboardInterrupt:
        print("\nInterrupted.")


async def _wrapped(coro) -> None:
    try:
        await coro
    finally:
        await close_dhan_client()


if __name__ == "__main__":
    main()
