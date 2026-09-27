"""Run the ORB strategy over 5 years of real history on an ACTUAL account.

This answers the only question that matters: with Rs 30,000 in the account and
"spend the balance on premium" sizing, what actually happens to the money?

    python run_algo_account_sim.py --balance 30000 --premium 115
    python run_algo_account_sim.py --balance 30000 --premium 115 --slippage 1.0
    python run_algo_account_sim.py --balance 30000 --mode risk --risk 2

Cost model = Dhan's real charge sheet, split into the part that does NOT depend
on quantity (brokerage) and the part that does (STT / exchange / stamp):

    brokerage  Rs 20 per order x 2
    exchange   ~0.05% of premium turnover, both legs
    GST        18% on (brokerage + exchange)
    STT        0.1% of the SELL-side premium value
    stamp      0.003% of the BUY-side premium value

For 1 lot NIFTY at Rs150 in / Rs226 out this lands at ~Rs77, which matches the
"about Rs80 per round trip" you see on a contract note.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Dict, List

from app import orb
from app.dhan_client import close_dhan_client
from app.instruments import lot_size_for
from app.orb_study import study

DELTA = 0.5

# ---- Dhan charge sheet ----
BROKERAGE_PER_ORDER = 20.0
EXCHANGE_RATE = 0.0005      # ~0.0495% NSE options premium, both legs
GST_RATE = 0.18             # on brokerage + exchange + SEBI
STT_RATE = 0.001            # 0.1% on sell-side premium value
STAMP_RATE = 0.00003        # 0.003% on buy-side premium value


def round_trip_cost(premium_in: float, premium_out: float, qty: int) -> float:
    """Real rupees charged by Dhan for one buy + one sell."""
    brokerage = BROKERAGE_PER_ORDER * 2
    exchange = EXCHANGE_RATE * (premium_in + premium_out) * qty
    gst = GST_RATE * (brokerage + exchange)
    stt = STT_RATE * premium_out * qty
    stamp = STAMP_RATE * premium_in * qty
    return brokerage + exchange + gst + stt + stamp


@dataclass
class SimResult:
    mode: str
    index: str
    trades: int
    skipped: int
    final: float
    peak: float
    max_dd: float
    max_dd_pct: float
    worst_day: float
    longest_loss_streak: int
    bleww_up: bool
    ruin_trade: int | None
    years: List[Dict[str, float]]
    avg_gross: float = 0.0
    avg_cost: float = 0.0
    avg_net: float = 0.0
    min_balance: float = 0.0
    worst_month: float = 0.0


def simulate(
    trades: List[orb.Trade],
    index: str,
    start_balance: float,
    entry_premium: float,
    mode: str,
    risk_pct: float = 2.0,
    slippage_rupees: float = 0.0,
    max_lots: int = 15,
    fixed_lots: int = 1,
) -> SimResult:
    lot = lot_size_for(index)
    balance = start_balance
    peak = balance
    max_dd = 0.0
    worst_day = 0.0
    streak = 0
    longest = 0
    blown = False
    ruin_trade = None
    by_year: Dict[int, Dict[str, float]] = {}
    taken = 0
    skipped = 0
    gross_sum = 0.0
    cost_sum = 0.0
    min_balance = balance
    months: Dict[str, float] = {}

    for t in trades:
        # ---- how many lots? ----
        if mode == "fixed":
            lots = fixed_lots
        elif mode == "full":
            # "buy as much as the balance allows" -> spend the whole balance
            lots = int(balance // (entry_premium * lot))
        else:
            risk_budget = balance * (risk_pct / 100.0)
            risk_per_lot = max(1e-9, abs(t.entry - t.stop)) * DELTA * lot
            cap_budget = balance
            lots = int(min(risk_budget / risk_per_lot, cap_budget // (entry_premium * lot)))
        lots = max(0, min(lots, max_lots))
        if lots == 0:
            skipped += 1
            if mode == "full" and not blown:
                blown = True
                ruin_trade = taken
            continue

        qty = lots * lot
        prem_in = entry_premium
        prem_out = entry_premium + t.points * DELTA        # spot move -> premium move
        if prem_out < 0.05:
            prem_out = 0.05
        cost = round_trip_cost(prem_in, prem_out, qty) + slippage_rupees
        gross = (prem_out - prem_in) * qty
        pnl = gross - cost
        gross_sum += gross
        cost_sum += cost

        balance += pnl
        taken += 1
        min_balance = min(min_balance, balance)
        mk = f"{t.d.year}-{t.d.month:02d}"
        months[mk] = months.get(mk, 0.0) + pnl
        if pnl < 0:
            streak += 1
            longest = max(longest, streak)
        else:
            streak = 0

        peak = max(peak, balance)
        max_dd = max(max_dd, peak - balance)
        worst_day = min(worst_day, pnl)

        y = by_year.setdefault(t.d.year, {"trades": 0, "pnl": 0.0, "end": 0.0})
        y["trades"] += 1
        y["pnl"] += pnl
        y["end"] = balance

        if balance <= 0:
            blown = True
            ruin_trade = taken
            break

    return SimResult(
        mode=mode, index=index, trades=taken, skipped=skipped,
        final=balance, peak=peak, max_dd=max_dd,
        max_dd_pct=(max_dd / peak * 100.0) if peak else 0.0,
        worst_day=worst_day, longest_loss_streak=longest,
        bleww_up=blown, ruin_trade=ruin_trade,
        years=[{"year": k, **v} for k, v in sorted(by_year.items())],
        avg_gross=(gross_sum / taken) if taken else 0.0,
        avg_cost=(cost_sum / taken) if taken else 0.0,
        avg_net=((gross_sum - cost_sum) / taken) if taken else 0.0,
        min_balance=min_balance,
        worst_month=min(months.values()) if months else 0.0,
    )


def report(r: SimResult, start_balance: float, entry_premium: float) -> None:
    s = "SURVIVED" if not r.bleww_up else "*** ACCOUNT WIPED OUT ***"
    print(f"\n  {s}")
    print(f"    start            Rs {start_balance:>12,.0f}")
    print(f"    end              Rs {r.final:>12,.0f}   ({(r.final/start_balance-1)*100:+.1f}%)")
    print(f"    LOWEST ever      Rs {r.min_balance:>12,.0f}   "
          f"({(r.min_balance/start_balance-1)*100:+.1f}% vs START)")
    print(f"    trades taken     {r.trades:>12,}   (skipped {r.skipped})")
    print(f"    max drawdown     Rs {r.max_dd:>12,.0f}   ({r.max_dd_pct:.1f}% from peak)")
    print(f"    worst single day Rs {r.worst_day:>12,.0f}")
    print(f"    longest losing streak   {r.longest_loss_streak} trades in a row")
    print(f"    worst single month    Rs {r.worst_month:>10,.0f}")
    print(f"    avg GROSS per trade   Rs {r.avg_gross:>10,.2f}")
    print(f"    avg COST per trade    Rs {r.avg_cost:>10,.2f}")
    print(f"    avg NET per trade     Rs {r.avg_net:>10,.2f}")
    if r.bleww_up:
        print(f"    wiped out at trade #{r.ruin_trade}")
    print(f"    --- year by year (premium assumed Rs{entry_premium:.0f}) ---")
    for y in r.years:
        print(f"    {y['year']}  {int(y['trades']):>4} trades   Rs {y['pnl']:>12,.0f}   "
              f"balance Rs {y['end']:>12,.0f}")


def monte_carlo(
    trades: List[orb.Trade],
    index: str,
    start_balance: float,
    entry_premium: float,
    mode: str,
    risk_pct: float,
    slippage_rupees: float,
    fixed_lots: int,
    n: int = 500,
    seed: int = 12345,
) -> Dict[str, Any]:
    """Shuffle the trade order N times.

    The strategy's result depends heavily on WHEN the 46-trade losing streak
    lands. If it lands early you are dead; if it lands after the account has
    grown you look like a genius. One historical path cannot tell you which -
    so we reshuffle and look at the whole distribution.
    """
    import random

    rng = random.Random(seed)
    finals: List[float] = []
    lows: List[float] = []
    dds: List[float] = []
    wiped = 0
    for _ in range(n):
        shuffled = list(trades)
        rng.shuffle(shuffled)
        r = simulate(shuffled, index, start_balance, entry_premium, mode,
                     risk_pct, slippage_rupees, fixed_lots=fixed_lots)
        finals.append(r.final)
        lows.append(r.min_balance)
        dds.append(r.max_dd)
        if r.final <= 0 or r.min_balance <= 0:
            wiped += 1

    def pct(xs: List[float], p: float) -> float:
        s = sorted(xs)
        return s[min(len(s) - 1, max(0, int(round(p / 100 * (len(s) - 1)))))]

    return {
        "n": n, "wiped": wiped,
        "wipe_pct": wiped / n * 100,
        "final_p05": pct(finals, 5), "final_p25": pct(finals, 25),
        "final_median": pct(finals, 50), "final_p75": pct(finals, 75),
        "final_p95": pct(finals, 95),
        "low_p05": pct(lows, 5), "low_median": pct(lows, 50),
        "dd_median": pct(dds, 50), "dd_p95": pct(dds, 95),
    }


def report_mc(mc: Dict[str, Any], start_balance: float) -> None:
    print(f"\n  === Monte Carlo: {mc['n']} shuffles (does the ORDER matter?) ===")
    print(f"    wiped out in       {mc['wipe_pct']:>8.1f}%  of paths")
    print(f"    final balance")
    print(f"       5th pct    Rs {mc['final_p05']:>12,.0f}")
    print(f"       25th pct   Rs {mc['final_p25']:>12,.0f}")
    print(f"       median     Rs {mc['final_median']:>12,.0f}")
    print(f"       75th pct   Rs {mc['final_p75']:>12,.0f}")
    print(f"       95th pct   Rs {mc['final_p95']:>12,.0f}")
    print(f"    lowest point")
    print(f"       5th pct    Rs {mc['low_p05']:>12,.0f}   <- the bad-luck start")
    print(f"       median     Rs {mc['low_median']:>12,.0f}")
    print(f"    max drawdown   median Rs {mc['dd_median']:>10,.0f}"
          f"   |  95th pct Rs {mc['dd_p95']:>10,.0f}")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--balance", type=float, default=30000)
    ap.add_argument("--premium", type=float, default=115.0,
                    help="ATM premium you actually pay (Rs30k/65/4 lots -> ~115)")
    ap.add_argument("--mode", choices=["full", "risk", "fixed"], default="full")
    ap.add_argument("--lots", type=int, default=1, help="lots for --mode fixed")
    ap.add_argument("--risk", type=float, default=2.0)
    ap.add_argument("--slippage", type=float, default=0.0,
                    help="extra rupees per round trip on top of the charge sheet")
    ap.add_argument("--years", type=int, default=5)
    ap.add_argument("--sl", type=float, default=0.2)
    ap.add_argument("--target", type=float, default=2.0)
    ap.add_argument("--mc", type=int, default=0,
                    help="Monte Carlo: reshuffle trade order N times")
    args = ap.parse_args()

    today = date.today()
    start = today - timedelta(days=int(args.years * 365.25) + 30)
    cfg = orb.StrategyConfig(
        target=orb.TargetRule(f"{args.target}x", "range_mult", args.target),
        sl_kind="range_mult", sl_value=args.sl,
    )

    print(f"Loading {args.years}y of history (cached) ...")
    for index in ("NIFTY", "SENSEX"):
        bars = await study._fetch(index, start, today)
        trades = orb.collect_trades(bars, cfg, index)
        lot = lot_size_for(index)
        print("\n" + "=" * 78)
        print(f"{index}  lot={lot}  stop={args.sl}x  target={args.target}x  "
              f"mode={args.mode}  premium=Rs{args.premium:.0f}  "
              f"slippage=Rs{args.slippage:.0f}  {len(trades)} trades over {args.years}y")
        print("=" * 78)
        report(
            simulate(trades, index, args.balance, args.premium, args.mode,
                     args.risk, args.slippage, fixed_lots=args.lots),
            args.balance, args.premium,
        )
        if args.mc:
            report_mc(
                monte_carlo(trades, index, args.balance, args.premium, args.mode,
                            args.risk, args.slippage, args.lots, n=args.mc),
                args.balance,
            )

    await close_dhan_client()


if __name__ == "__main__":
    asyncio.run(main())
