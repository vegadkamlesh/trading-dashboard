"""ORB backtest on real option premiums (run run_history_cache.py, run_expiry_calendar.py,
run_option_cache.py first).

    .venv\\Scripts\\python.exe run_orb_option_backtest.py     # writes data/history/orb_option_report.md

Replays the live algo minute by minute:
  * OR 09:15-09:24, levels lock 09:25; NIFTY and SENSEX both watched, first break takes the trade
    (SENSEX only from its weekly-options era, May-2023).
  * Break up -> nearest-expiry CALL at the ATM strike of the OR high; down -> PUT at OR low.
  * Entry premium = that option's 1-minute close in the breakout minute (fills are not tick exact).
  * Up to MAX_TRADES per day; the next one needs a fresh break (spot back inside first); entries until 15:00.
  * Exits: premium target %, premium stop %, optional spot stop at the opposite OR level, optional spot target
    (k x OR range), broker protective SL at -70% premium, and 15:29.
    When a stop and a target fall in the same minute the stop is assumed first (conservative).
Live sizing puts ~90% of the balance into premium, so a rupee box equals a premium % move times the
deployed capital: results are in % of premium (= % of deployed capital) net of COST_PCT per round trip.
"""
from __future__ import annotations

import csv
import gzip
import json
import statistics as st
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import date
from itertools import product
from pathlib import Path

from run_orb_market_study import load, load_calendar

ROOT = Path(__file__).resolve().parent
HIST = ROOT / "data" / "history"
OR_START, OR_END, CUTOFF, EOD = 555, 564, 900, 929
INDICES = ("NIFTY", "SENSEX")
SENSEX_FROM = "2023-05-15"
# Round trip, % of premium: brokerage+GST on ~Rs50k deployed ~0.1, STT 0.1 (sell), exchange+GST ~0.08,
# stamp 0.003, plus ~0.3% slippage per side.
COST_PCT = 0.9
PROTECTIVE = 0.70
SPLIT = "2025-01-01"            # rules are judged on data before SPLIT and checked after it
MIN_TRADES = 200
TGTS = [5, 10, 15, 20, 25, 30, 40, 50, 75, 100, None]
SLS = [5, 10, 15, 20, 25, 30, 40, 50, None]


@dataclass(frozen=True)
class Rule:
    tgt: float | None           # premium target %
    sl: float | None            # premium stop %
    spot_sl: bool = False       # exit when spot crosses the opposite OR level
    spot_tgt: float | None = 2.0  # exit at entry spot +/- k x OR range (live default 2.0)
    max_trades: int = 2

    def label(self) -> str:
        parts = [f"T {self.tgt:g}%" if self.tgt else "T off", f"SL {self.sl:g}%" if self.sl else "SL off (70% only)"]
        if self.spot_sl:
            parts.append("spot-SL ulta OR")
        parts.append(f"spot-T {self.spot_tgt:g}xOR" if self.spot_tgt else "spot-T off")
        if self.max_trades != 2:
            parts.append(f"{self.max_trades} trade/din")
        return ", ".join(parts)


# ---------------------------------------------------------------- data
def _arr(bars):
    a = [None] * (EOD + 1 - OR_START)
    for b in bars:
        if OR_START <= b[0] <= EOD:
            a[b[0] - OR_START] = b
    return a


def load_days() -> list[dict]:
    opt: dict[tuple, dict] = {}
    for key in INDICES:
        p = HIST / f"{key}_opt_1m.csv.gz"
        if not p.exists():
            sys.exit(f"{p} missing — run run_option_cache.py first.")
        with gzip.open(p, "rt", newline="") as f:
            r = csv.reader(f)
            next(r)
            for day, side, strike, m, o, h, l, c, iv, sp in r:
                opt.setdefault((key, day, side), {"strike": float(strike), "bars": []})["bars"].append(
                    (int(m), float(o), float(h), float(l), float(c), float(iv)))
    days: dict[str, dict] = defaultdict(dict)
    for key in INDICES:
        done_p = HIST / f"{key}_opt_done.json"
        done = set(json.loads(done_p.read_text())) if done_p.exists() else set()
        spot = load(key)
        exps, ws = load_calendar(key)
        prev_close = None
        for d in sorted(spot):
            bars = spot[d]
            day = d.isoformat()
            orb = [b for b in bars if OR_START <= b[0] <= OR_END]
            ok = len(orb) == 10 and sum(1 for b in bars if OR_END < b[0] <= EOD) >= 200
            if ok and day in done and (key == "NIFTY" or day >= SENSEX_FROM):
                hi, lo = max(b[2] for b in orb), min(b[3] for b in orb)
                days[day][key] = {
                    "hi": hi, "lo": lo, "or_pct": (hi - lo) / lo * 100, "spot": _arr(bars),
                    "gap": (orb[0][1] - prev_close) / prev_close * 100 if prev_close else 0.0,
                    "expiry": day in exps and day >= ws,
                    "opt": {s: _arr(opt[(key, day, s)]["bars"]) for s in ("CALL", "PUT") if (key, day, s) in opt},
                }
            if bars:
                prev_close = bars[-1][4]
    return [dict(day=d, **v) for d, v in sorted(days.items()) if any(k in v for k in INDICES)]


# ---------------------------------------------------------------- simulation
def simulate(day: dict, rule: Rule) -> list[dict]:
    idx = [k for k in INDICES if k in day]
    trades, m, need_fresh = [], OR_END + 1, {}
    while len(trades) < rule.max_trades and m <= CUTOFF:
        hit = None
        for mm in range(m, CUTOFF + 1):
            for k in idx:
                b = day[k]["spot"][mm - OR_START]
                if b is None:
                    continue
                for side, broke, inside in (("CALL", b[2] > day[k]["hi"], b[4] <= day[k]["hi"]),
                                            ("PUT", b[3] < day[k]["lo"], b[4] >= day[k]["lo"])):
                    if need_fresh.get((k, side)):
                        if inside:
                            need_fresh[(k, side)] = False
                        continue
                    if broke:
                        hit = (mm, k, side)
                        break
                if hit:
                    break
            if hit:
                break
        if not hit:
            break
        mm, k, side = hit
        t = _trade(day, k, side, mm, rule)
        t["n"] = len(trades) + 1
        trades.append(t)
        x = t["exit_min"]
        for kk in idx:
            sb = day[kk]["spot"][min(x, EOD) - OR_START]
            if sb:
                need_fresh[(kk, "CALL")] = sb[4] > day[kk]["hi"]
                need_fresh[(kk, "PUT")] = sb[4] < day[kk]["lo"]
        m = x + 1
    return trades


def _trade(day: dict, k: str, side: str, mm: int, rule: Rule) -> dict:
    d = day[k]
    base = {"day": day["day"], "index": k, "side": side, "entry_min": mm, "or_pct": d["or_pct"],
            "gap": d["gap"], "expiry": d["expiry"]}
    opt = d["opt"].get(side)
    e_bar = next((opt[i - OR_START] for i in range(mm, min(mm + 3, EOD + 1)) if opt and opt[i - OR_START]), None)
    if not e_bar or e_bar[4] <= 0:
        return {**base, "exit_min": mm, "ret": None, "reason": "NODATA", "iv": None}
    e = e_bar[4]
    rng = d["hi"] - d["lo"]
    e_spot = d["hi"] if side == "CALL" else d["lo"]
    up = side == "CALL"
    last, reason, px, x = e, "EOD", None, EOD
    hi_px, lo_px = e, e
    for x in range(mm + 1, EOD + 1):
        ob, sb = opt[x - OR_START], d["spot"][x - OR_START]
        if ob:
            hi_px, lo_px = max(hi_px, ob[2]), min(lo_px, ob[3])
        if ob:
            if rule.sl and ob[3] <= e * (1 - rule.sl / 100):
                px, reason = min(ob[1], e * (1 - rule.sl / 100)), "SL"
            elif ob[3] <= e * (1 - PROTECTIVE):
                px, reason = min(ob[1], e * (1 - PROTECTIVE)), "SL70"
        if px is None and rule.spot_sl and sb and ((sb[3] < d["lo"]) if up else (sb[2] > d["hi"])):
            px, reason = (ob[4] if ob else last), "SPOT_SL"
        if px is None and ob and rule.tgt and ob[2] >= e * (1 + rule.tgt / 100):
            px, reason = max(ob[1], e * (1 + rule.tgt / 100)), "TARGET"
        if px is None and rule.spot_tgt and sb and (
                (sb[2] >= e_spot + rule.spot_tgt * rng) if up else (sb[3] <= e_spot - rule.spot_tgt * rng)):
            px, reason = (ob[4] if ob else last), "SPOT_T"
        if ob:
            last = ob[4]
        if px is not None:
            break
    if px is None:
        px = last
    return {**base, "exit_min": x, "entry": e, "exit": px, "reason": reason, "iv": e_bar[5],
            "mfe": (hi_px - e) / e * 100, "mae": (lo_px - e) / e * 100,
            "ret": (px - e) / e * 100 - COST_PCT}


def run_rule(args) -> tuple[Rule, list[dict]]:
    rule, days = args
    return rule, [t for d in days for t in simulate(d, rule)]


# ---------------------------------------------------------------- metrics
def metrics(trades: list[dict]) -> dict:
    tr = [t for t in trades if t["ret"] is not None]
    if not tr:
        return {}
    r = [t["ret"] for t in tr]
    by_day = defaultdict(float)
    for t in tr:
        by_day[t["day"]] += t["ret"]
    eq = peak = dd = streak = worst_streak = 0.0
    for v in by_day.values():
        eq += v
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
        streak = streak + 1 if v < 0 else 0
        worst_streak = max(worst_streak, streak)
    wins, losses = [x for x in r if x > 0], [x for x in r if x <= 0]
    dv = list(by_day.values())
    return {"n": len(tr), "days": len(dv), "win": len(wins) / len(r) * 100, "avg": st.mean(r),
            "avg_win": st.mean(wins) if wins else 0.0, "avg_loss": st.mean(losses) if losses else 0.0,
            "pf": sum(wins) / -sum(losses) if losses and sum(losses) else 99.0,
            "day_win": sum(1 for v in dv if v > 0) / len(dv) * 100, "worst_day": min(dv), "best_day": max(dv),
            "dd": dd, "streak": int(worst_streak), "total": sum(dv),
            "nodata": sum(1 for t in trades if t["ret"] is None)}


def split(trades, lo="", hi="9999"):
    return [t for t in trades if lo <= t["day"] < hi]


def row(label: str, m: dict, extra: str = "") -> str:
    if not m:
        return f"| {label} | – | – | – | – | – | – | – |{extra}"
    return (f"| {label} | {m['n']} | {m['win']:.0f} | +{m['avg_win']:.1f} / {m['avg_loss']:.1f} | **{m['avg']:+.2f}** "
            f"| {m['pf']:.2f} | {m['worst_day']:.0f} | {m['dd']:.0f} |{extra}")


HDR = ("| Rule | Trades | Win % | Avg win / loss % | **Avg % / trade** | Profit factor | Worst din % | Max drawdown % |",
       "|---|---|---|---|---|---|---|---|")


def buckets(trades: list[dict]) -> list[str]:
    tr = [t for t in trades if t["ret"] is not None]
    defs = [
        ("OR chaudai", lambda t: "a <0.2%" if t["or_pct"] < .2 else "b 0.2–0.35%" if t["or_pct"] < .35
         else "c 0.35–0.5%" if t["or_pct"] < .5 else "d >0.5%"),
        ("Gap open", lambda t: "a flat <0.2%" if abs(t["gap"]) <= .2 else "b 0.2–0.5%" if abs(t["gap"]) <= .5 else "c >0.5%"),
        ("Gap vs breakout", lambda t: "a no gap" if abs(t["gap"]) <= .2 else
         "b gap ki direction me" if (t["gap"] > 0) == (t["side"] == "CALL") else "c gap ke ulta"),
        ("Entry time", lambda t: "a 09:25–09:30" if t["entry_min"] <= 570 else "b 09:31–10:00" if t["entry_min"] <= 600
         else "c 10:01–11:00" if t["entry_min"] <= 660 else "d 11:00 ke baad"),
        ("Din", lambda t: "b Expiry" if t["expiry"] else "a Normal"),
        ("Trade no.", lambda t: f"Trade {t['n']}"),
        ("Index / side", lambda t: f"{t['index']} {t['side']}"),
    ]
    out = ["| Group | Bucket | Train trades | Train win % | **Train avg %** | Test trades | Test win % | **Test avg %** |",
           "|---|---|---|---|---|---|---|---|"]
    for name, f in defs:
        g = defaultdict(lambda: ([], []))
        for t in tr:
            g[f(t)][0 if t["day"] < SPLIT else 1].append(t["ret"])
        for b in sorted(g):
            a, c = g[b]
            cell = lambda v: (f"{len(v)} | {sum(1 for x in v if x > 0) / len(v) * 100:.0f} | **{st.mean(v):+.2f}**"
                              if v else "0 | – | –")
            out.append(f"| {name} | {b.split(' ', 1)[1] if b[1] == ' ' else b} | {cell(a)} | {cell(c)} |")
    return out


def excursion(trades: list[dict]) -> list[str]:
    tr = [t for t in trades if t["ret"] is not None]
    n = len(tr)
    out = ["| Premium move | ₹ per ₹10,000 lagaye | Kam se kam itna **upar** gaya (100 me se) "
           "| Kam se kam itna **niche** gaya (100 me se) |", "|---|---|---|---|"]
    for x in (5, 10, 15, 20, 25, 30, 40, 50, 70, 100):
        up = sum(1 for t in tr if t["mfe"] >= x) / n * 100
        dn = sum(1 for t in tr if t["mae"] <= -x) / n * 100
        out.append(f"| {x}% | ₹{x * 100:,} | {up:.0f} | {dn:.0f} |")
    return out


EDGES = [0, 5, 10, 20, 30, 50, 100]


def hundred(trades: list[dict]) -> list[str]:
    """Exclusive buckets that add up to 100: the furthest the premium went up / down after entry."""
    tr = [t for t in trades if t["ret"] is not None]
    n = len(tr)
    out = ["| Premium ka max move (entry se) | ₹ per ₹10,000 | **Upar** itna gaya (100 me se) "
           "| **Niche** itna gaya (100 me se) |", "|---|---|---|---|"]
    for lo, hi in zip(EDGES, EDGES[1:] + [None]):
        f = (lambda v: v >= lo) if hi is None else (lambda v: lo <= v < hi)
        lab = f"{lo}%+" if hi is None else f"{lo}–{hi}%"
        rs = f"₹{lo * 100:,}+" if hi is None else f"₹{lo * 100:,}–{hi * 100:,}"
        out.append(f"| {lab} | {rs} | {sum(1 for t in tr if f(t['mfe'])) / n * 100:.0f} "
                   f"| {sum(1 for t in tr if f(-t['mae'])) / n * 100:.0f} |")
    return out


def outcome_table(res: dict, pairs: list[tuple]) -> list[str]:
    """Per 100 trades: target first / stop first / neither (closed 15:29), and the average."""
    out = ["| Target / SL (premium %) | **Target pehle** | **SL pehle** | 15:29 band (dono nahi) | Avg % / trade "
           "| ₹ per ₹10,000 per trade |", "|---|---|---|---|---|---|"]
    for t, s in pairs:
        tr = [x for x in res[Rule(t, s, False, None, 1)] if x["ret"] is not None]
        if not tr:
            continue
        c = lambda *why: sum(1 for x in tr if x["reason"] in why) / len(tr) * 100
        avg = st.mean(x["ret"] for x in tr)
        out.append(f"| {t}% / {s}% | {c('TARGET'):.0f} | {c('SL', 'SL70'):.0f} | {c('EOD'):.0f} "
                   f"| {avg:+.2f} | ₹{avg * 100:+,.0f} |")
    return out


def day_loss(trades: list[dict]) -> list[str]:
    """Daily result buckets per 100 trading days plus the tail, used to size the daily loss box."""
    by_day = defaultdict(float)
    for t in trades:
        if t["ret"] is not None:
            by_day[t["day"]] += t["ret"]
    v = sorted(by_day.values())
    n = len(v)
    q = lambda p: v[min(n - 1, int(p * n))]
    cuts = [(None, -30), (-30, -20), (-20, -10), (-10, 0), (0, 10), (10, 20), (20, None)]
    out = ["| Din ka result (% of deployed) | 100 din me se |", "|---|---|"]
    for lo, hi in cuts:
        k = sum(1 for x in v if (lo is None or x >= lo) and (hi is None or x < hi))
        lab = f"< {hi}%" if lo is None else f"{lo}% se upar" if hi is None else f"{lo}% – {hi}%"
        out.append(f"| {lab} | {k / n * 100:.0f} |")
    out += ["", f"Sabse bura din {v[0]:.1f}%, 100 me se sabse bure 5 din ki seema {q(.05):.1f}%, "
                f"sabse bure 1 din ki seema {q(.01):.1f}%."]
    return out


def main() -> None:
    days = load_days()
    rules = [Rule(t, s, sp, stg) for t, s, sp, stg in product(TGTS, SLS, (False, True), (2.0, None))]
    rules += [Rule(t, s, sp, stg, 1) for t, s, sp, stg in product(TGTS, SLS, (False, True), (2.0, None))]
    with ProcessPoolExecutor() as pool:
        res = dict(pool.map(run_rule, [(r, days) for r in rules], chunksize=4))
    live = Rule(None, None, False, 2.0, 2)
    train = {r: metrics(split(t, hi=SPLIT)) for r, t in res.items()}
    test = {r: metrics(split(t, lo=SPLIT)) for r, t in res.items()}
    full = {r: metrics(t) for r, t in res.items()}
    ok = [r for r in rules if train[r] and train[r]["n"] >= MIN_TRADES]
    top = sorted(ok, key=lambda r: train[r]["avg"], reverse=True)[:15]
    safe = sorted([r for r in ok if r.sl], key=lambda r: (train[r]["worst_day"], train[r]["avg"]), reverse=True)[:8]

    first, last = days[0]["day"], days[-1]["day"]
    rep = [f"# ORB backtest on real option premiums ({first} → {last})", "",
           f"Dhan expired-options 1-min data, exact strike the live algo picks. Result = % of premium "
           f"(= % of deployed capital) after ~{COST_PCT}% round-trip cost. Rules chosen on data before {SPLIT}, "
           f"checked on {SPLIT} onwards.", ""]
    rep += ["## Abhi wala live setup", "", *HDR,
            row(f"Live (T off, SL off, spot-T 2xOR) — {first[:4]}–2024", train[live]),
            row(f"Live — {SPLIT[:4]} se", test[live]), row("Live — poora", full[live]), ""]
    rep += ["## Top 15 rules (2021–2024 par chune) aur unka 2025+ result", "",
            "| Rule | Train avg % | Train win % | Train worst din % | **Test avg %** | Test win % | Test worst din % | Test drawdown % |",
            "|---|---|---|---|---|---|---|---|"]
    for r in top:
        a, b = train[r], test[r]
        rep.append(f"| {r.label()} | {a['avg']:+.2f} | {a['win']:.0f} | {a['worst_day']:.0f} | **{b.get('avg', 0):+.2f}** "
                   f"| {b.get('win', 0):.0f} | {b.get('worst_day', 0):.0f} | {b.get('dd', 0):.0f} |")
    rep += ["", "## Sabse kam nuksaan wale rules (worst din sabse chhota)", "", *HDR]
    rep += [row(r.label() + " — poora", full[r]) for r in safe]
    rep += ["", "## Grid: avg % per trade (poora data, 2 trade/din, spot-SL off, spot-T off)", "",
            "| Target \\ SL | " + " | ".join(f"{s}%" if s else "off" for s in SLS) + " |",
            "|---|" + "---|" * len(SLS)]
    for t in TGTS:
        rep.append(f"| {t if t else 'off'}{'%' if t else ''} | " + " | ".join(
            f"{full[Rule(t, s, False, None)].get('avg', 0):+.2f}" for s in SLS) + " |")
    hold = Rule(None, None, False, None, 1)
    rep += ["", "## 100 trade me se premium kitna upar / niche gaya (pehla breakout, 15:29 tak hold, -70% SL)", "",
            "Buckets — jod ke 100:", "", *hundred(res[hold]), "",
            "Cumulative — kam se kam itna:", "", *excursion(res[hold]), ""]
    rep += ["## 100 trade me se: target pehle aaya ya SL (1 trade/din, spot exits off)", "",
            *outcome_table(res, [(t, s) for t in (10, 15, 20, 30, 50) for s in (10, 15, 20, 30)]), ""]
    rep += ["## Din ka nuksaan — max-loss box ke liye (rule: " + top[0].label() + ")", "",
            *day_loss(res[top[0]]), "", "## Din ka nuksaan — abhi wala live rule", "", *day_loss(res[live]), ""]
    best = top[0]
    rep += ["", f"## Filter check — kaunse din skip karne layak (rule: {best.label()})", "",
            "Ek filter tabhi bharose layak hai jab train aur test dono me same direction dikhe.", "",
            *buckets(res[best]), ""]
    rep += ["", "## Filter check — abhi wala live rule", "", *buckets(res[live]), ""]
    out = HIST / "orb_option_report.md"
    json.dump({"rules": [[r.label(), full[r], train[r], test[r]] for r in rules]},
              open(HIST / "orb_option_grid.json", "w"), indent=0, default=str)
    out.write_text("\n".join(rep), encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    print("\n".join(rep))
    print(f"\nWritten {out}")


if __name__ == "__main__":
    main()
