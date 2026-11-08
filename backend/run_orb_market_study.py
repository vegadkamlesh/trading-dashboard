"""ORB market study on the local 1-minute cache (run run_history_cache.py first).

    .venv\\Scripts\\python.exe run_orb_market_study.py      # writes data/history/orb_market_report.md

OR = 09:15:00-09:24:59 (10 candles), same as the live algo. Post-OR window = 09:25-15:29.
Days without both the 09:15 and 09:24 candles (holidays, Muhurat, data gaps) are skipped.
Expiry days come from data/history/expiry_calendar.json (run_expiry_calendar.py), built from
the real NSE/BSE F&O bhavcopies, so holiday and one-off exchange shifts are included. The
expiry comparison only uses each index's weekly-expiry era (NIFTY Feb-2019, SENSEX May-2023).
"""
from __future__ import annotations

import csv
import gzip
import json
import statistics as st
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HIST = ROOT / "data" / "history"
OR_START, OR_END, EOD = 9 * 60 + 15, 9 * 60 + 24, 15 * 60 + 29
EXT_BUCKETS = [0.0, 0.05, 0.1, 0.15, 0.2, 0.3, 0.5, 0.75, 1.0, 1.5]
TARGETS = [0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5, 0.75, 1.0]
DELTA = {"normal": 0.5, "expiry": 0.6}


def load(key: str) -> dict[date, list[tuple]]:
    days: dict[date, list[tuple]] = defaultdict(list)
    with gzip.open(HIST / f"{key}_1m.csv.gz", "rt", newline="") as f:
        r = csv.reader(f)
        next(r)
        for ts, o, h, l, c in r:
            m = int(ts[11:13]) * 60 + int(ts[14:16])
            days[date.fromisoformat(ts[:10])].append((m, float(o), float(h), float(l), float(c)))
    return days


# ---------------------------------------------------------------- expiry calendar
CALENDAR = HIST / "expiry_calendar.json"
# (label, from, to) — weekday regimes of the weekly contract, from the exchange calendar.
REGIMES = {
    "NIFTY": [("Thu weekly", "2019-02-14", "2025-08-31"), ("Tue weekly", "2025-09-01", "9999")],
    "SENSEX": [("Fri weekly", "2023-05-19", "2024-12-31"), ("Tue weekly", "2025-01-01", "2025-08-31"),
               ("Thu weekly", "2025-09-01", "9999")],
}


def load_calendar(key: str) -> tuple[set[str], str]:
    """Exchange expiry dates and the first weekly-era expiry for one index."""
    if not CALENDAR.exists():
        sys.exit(f"{CALENDAR} missing — run run_expiry_calendar.py first.")
    c = json.loads(CALENDAR.read_text(encoding="utf-8"))[key]
    return set(c["expiries"]), c["weekly_start"]


def expiry_weekday(key: str, d: date) -> int | None:
    """Old weekday rule, kept only to report how many days it misclassified.
    0=Mon .. 4=Fri, or None when the index had no liquid weekly contract."""
    if key == "NIFTY":
        if d < date(2019, 2, 11):
            return None
        return 3 if d < date(2025, 9, 1) else 1          # Thu -> Tue (Sep-2025)
    if d < date(2023, 5, 15):
        return None                                      # SENSEX weekly relaunch May-2023
    if d < date(2025, 1, 1):
        return 4                                         # Fri
    return 1 if d < date(2025, 9, 1) else 3              # Tue -> Thu (Sep-2025)


def expiry_days(key: str, trading: set[date]) -> set[date]:
    out = set()
    weeks = defaultdict(list)
    for d in trading:
        weeks[d.isocalendar()[:2]].append(d)
    for ds in weeks.values():
        wd = expiry_weekday(key, ds[0])
        if wd is None:
            continue
        monday = ds[0] - timedelta(days=ds[0].weekday())
        target = monday + timedelta(days=wd)
        cands = [d for d in ds if d <= target]
        if cands:
            out.add(max(cands))
    return out


# ---------------------------------------------------------------- per-day metrics
def analyse_day(bars: list[tuple]) -> dict | None:
    mins = {b[0] for b in bars}
    if OR_START not in mins or OR_END not in mins:
        return None
    orb = [b for b in bars if OR_START <= b[0] <= OR_END]
    post = [b for b in bars if OR_END < b[0] <= EOD]
    if len(post) < 200:
        return None
    hi, lo = max(b[2] for b in orb), min(b[3] for b in orb)
    d = {"or_h": hi, "or_l": lo, "or_pct": (hi - lo) / lo * 100, "close": post[-1][4]}
    d["up_ext"] = max(0.0, max(b[2] for b in post) - hi) / hi * 100
    d["dn_ext"] = max(0.0, lo - min(b[3] for b in post)) / lo * 100

    first, first_min, up_t, dn_t = None, None, None, None
    for m, o, h, l, c in post:
        if up_t is None and h > hi:
            up_t = m
        if dn_t is None and l < lo:
            dn_t = m
        if first is None and (up_t is not None or dn_t is not None):
            if up_t is not None and dn_t is not None:       # one candle broke both sides
                first = "UP" if c >= o else "DOWN"
            else:
                first = "UP" if up_t is not None else "DOWN"
            first_min = m
    d.update(first=first, first_min=first_min, both=up_t is not None and dn_t is not None)
    if first is None:
        return d

    level = hi if first == "UP" else lo
    sign = 1 if first == "UP" else -1
    after = [b for b in post if b[0] >= first_min]
    # Hit-before-reverse: target reached before spot touches the OPPOSITE OR level.
    opp = lo if first == "UP" else hi
    hits = {}
    for t in TARGETS:
        goal = level * (1 + sign * t / 100)
        res = "none"
        for m, o, h, l, c in after:
            reached = h >= goal if sign > 0 else l <= goal
            reversed_ = l <= opp if sign > 0 else h >= opp
            if reversed_:                                  # same candle -> count as loss (conservative)
                res = "reverse"
                break
            if reached:
                res = "hit"
                break
        hits[t] = res
    d["hits"] = hits
    # Live target-only rule: target any time before 15:29 (no software stop), else 15:29 close.
    best = max(b[2] for b in after) if sign > 0 else min(b[3] for b in after)
    d["hit_any"] = {t: sign * (best - level) / level * 100 >= t for t in TARGETS}
    d["or_dist"] = (hi - lo) / level * 100
    d["fav"] = max(0.0, sign * ((max(b[2] for b in after) if sign > 0 else min(b[3] for b in after)) - level)) / level * 100
    worst = min(b[3] for b in after) if sign > 0 else max(b[2] for b in after)
    d["adv"] = max(0.0, sign * (level - worst)) / level * 100
    d["full_reverse"] = (worst <= opp) if sign > 0 else (worst >= opp)
    d["eod_pct"] = sign * (post[-1][4] - level) / level * 100

    # Re-breaks: a new break needs a close back inside the range first (same as live re-arm).
    for side, lv, sg in (("UP", hi, 1), ("DOWN", lo, -1)):
        breaks, armed, cur = [], True, None
        for m, o, h, l, c in post:
            crossed = h > lv if sg > 0 else l < lv
            if armed and crossed:
                armed, cur = False, 0.0
                breaks.append(0.0)
            if cur is not None:
                ext = sg * ((h if sg > 0 else l) - lv) / lv * 100
                breaks[-1] = max(breaks[-1], ext)
            inside = c <= lv if sg > 0 else c >= lv
            if not armed and inside:
                armed, cur = True, None
        d[f"breaks_{side}"] = breaks
    return d


# ---------------------------------------------------------------- formatting
def pct(n: int, tot: int) -> str:
    return f"{100 * n / tot:5.1f}%" if tot else "  -  "


def q(vals: list[float], p: float) -> float:
    s = sorted(vals)
    return s[min(len(s) - 1, int(round(p * (len(s) - 1))))] if s else 0.0


def ext_table(rows: list[dict], key: str, spot: float, delta: float) -> list[str]:
    vals = [r[key] for r in rows]
    out = ["| Spot ne OR level se kitna aage gaya | Din | % din | Spot pts (aaj ke level par) | Option pts (approx) |",
           "|---|---|---|---|---|"]
    edges = EXT_BUCKETS + [float("inf")]
    for a, b in zip(edges, edges[1:]):
        if a == 0.0:
            n = sum(1 for v in vals if v == 0.0)
            out.append(f"| break hi nahi hua | {n} | {pct(n, len(vals))} | 0 | 0 |")
            n = sum(1 for v in vals if 0 < v < 0.05)
            out.append(f"| 0 – 0.05% | {n} | {pct(n, len(vals))} | 0–{spot * 0.0005:.0f} | 0–{spot * 0.0005 * delta:.0f} |")
            continue
        n = sum(1 for v in vals if a <= v < b)
        lab = f"{a:g} – {b:g}%" if b != float("inf") else f"> {a:g}%"
        sp = f"{spot * a / 100:.0f}–{spot * b / 100:.0f}" if b != float("inf") else f"> {spot * a / 100:.0f}"
        op = (f"{spot * a / 100 * delta:.0f}–{spot * b / 100 * delta:.0f}" if b != float("inf")
              else f"> {spot * a / 100 * delta:.0f}")
        out.append(f"| {lab} | {n} | {pct(n, len(vals))} | {sp} | {op} |")
    return out


def target_table(rows: list[dict], spot: float, lot: int) -> list[str]:
    tr = [r for r in rows if r["first"]]
    exp = [r for r in tr if r["expiry"]]
    out = ["| Target (spot % breakout level se) | Spot pts | Option pts ~ (normal / expiry) | ₹ per 1 lot (normal) "
           "| Hit hua (sab din) | Hit (expiry din) | Pehle ulta OR toda | Na hit, na reverse (15:29 tak) |",
           "|---|---|---|---|---|---|---|---|"]
    for t in TARGETS:
        hit = sum(1 for r in tr if r["hits"][t] == "hit")
        rev = sum(1 for r in tr if r["hits"][t] == "reverse")
        eh = sum(1 for r in exp if r["hits"][t] == "hit")
        sp = spot * t / 100
        out.append(f"| {t:g}% | {sp:.0f} | {sp * DELTA['normal']:.0f} / {sp * DELTA['expiry']:.0f} | "
                   f"₹{sp * DELTA['normal'] * lot:,.0f} | {pct(hit, len(tr))} | {pct(eh, len(exp))} | "
                   f"{pct(rev, len(tr))} | {pct(len(tr) - hit - rev, len(tr))} |")
    return out


def rebreak_table(rows: list[dict]) -> list[str]:
    out = ["| Side | 0 baar | 1 baar | 2 baar | 3 baar | 4+ baar | 2nd break ka median ext | 3rd break ka median ext |",
           "|---|---|---|---|---|---|---|---|"]
    for side, lab in (("UP", "High ke upar"), ("DOWN", "Low ke niche")):
        cnt = [len(r.get(f"breaks_{side}", [])) for r in rows]
        sec = [r.get(f"breaks_{side}", [])[1] for r in rows if len(r.get(f"breaks_{side}", [])) > 1]
        thr = [r.get(f"breaks_{side}", [])[2] for r in rows if len(r.get(f"breaks_{side}", [])) > 2]
        c = lambda k: sum(1 for x in cnt if x == k)
        out.append(f"| {lab} | {pct(c(0), len(cnt))} | {pct(c(1), len(cnt))} | {pct(c(2), len(cnt))} | "
                   f"{pct(c(3), len(cnt))} | {pct(sum(1 for x in cnt if x >= 4), len(cnt))} | "
                   f"{st.median(sec) if sec else 0:.2f}% | {st.median(thr) if thr else 0:.2f}% |")
    return out


def ev_table(rows: list[dict], spot: float, cost: float = 0.02) -> list[str]:
    """Average result per trade in spot %, after an approximate round-trip cost (spot %)."""
    tr = [r for r in rows if r["first"]]
    out = ["| Target | A: abhi wala rule (target ya 15:29) — win% | A: avg win / avg loss | **A: avg per trade** "
           "| B: SL = ulta OR level — win% | **B: avg per trade** |",
           "|---|---|---|---|---|---|"]
    for t in TARGETS:
        a = [t if r["hit_any"][t] else r["eod_pct"] for r in tr]
        b = [t if r["hits"][t] == "hit" else (-r["or_dist"] if r["hits"][t] == "reverse" else r["eod_pct"])
             for r in tr]
        a = [x - cost for x in a]
        b = [x - cost for x in b]
        aw, al = [x for x in a if x > 0], [x for x in a if x <= 0]
        out.append(f"| {t:g}% ({spot * t / 100:.0f} spot pts) | {pct(len(aw), len(a))} | "
                   f"+{st.mean(aw) if aw else 0:.2f}% / {st.mean(al) if al else 0:.2f}% | "
                   f"**{st.mean(a):+.3f}%** ({spot * st.mean(a) / 100:+.1f} pts) | "
                   f"{pct(sum(1 for x in b if x > 0), len(b))} | **{st.mean(b):+.3f}%** ({spot * st.mean(b) / 100:+.1f} pts) |")
    return out


def section(key: str, rows: list[dict], label: str, spot: float, lot: int) -> list[str]:
    n = len(rows)
    tr = [r for r in rows if r["first"]]
    up = [r for r in tr if r["first"] == "UP"]
    dn = [r for r in tr if r["first"] == "DOWN"]
    both = [r for r in rows if r["both"]]
    rev = [r for r in tr if r["full_reverse"]]
    eod_win = [r for r in tr if r["eod_pct"] > 0]
    fm = sorted(r["first_min"] for r in tr)
    hhmm = lambda m: f"{m // 60:02d}:{m % 60:02d}"
    or_w = [r["or_pct"] for r in rows]
    out = [f"### {key} — {label} ({n} din, {rows[0]['day']} → {rows[-1]['day']})", "",
           f"- OR (10 min) ki chaudai: median **{st.median(or_w):.2f}%** (~{spot * st.median(or_w) / 100:.0f} spot pts), "
           f"25–75%: {q(or_w, .25):.2f}% – {q(or_w, .75):.2f}%",
           f"- Pehla break **UP {pct(len(up), n)}**, **DOWN {pct(len(dn), n)}**, koi break nahi {pct(n - len(tr), n)}",
           f"- Pehla break kab: median **{hhmm(int(st.median(fm)))}**; 09:30 tak {pct(sum(1 for m in fm if m <= 570), len(fm))}, "
           f"10:00 tak {pct(sum(1 for m in fm if m <= 600), len(fm))}, 11:00 ke baad {pct(sum(1 for m in fm if m > 660), len(fm))}",
           f"- **Dono side break (upar bhi, niche bhi) same din: {pct(len(both), n)}**",
           f"- Pehle break ke baad spot **ulta OR ka dusra level bhi tod gaya** (full reversal): {pct(len(rev), len(tr))}",
           f"- 15:29 par spot breakout level se **favour me** band: {pct(len(eod_win), len(tr))} "
           f"(median result {st.median([r['eod_pct'] for r in tr]):+.2f}%)",
           f"- Breakout ke baad max favour move: median **{st.median([r['fav'] for r in tr]):.2f}%**, "
           f"max ulta move (level se peeche): median **{st.median([r['adv'] for r in tr]):.2f}%**", "",
           "**High ke upar gaya to kitna (poore din ka max, OR high se):**", ""]
    out += ext_table(rows, "up_ext", spot, DELTA["normal"])
    out += ["", "**Low ke niche gaya to kitna (poore din ka max, OR low se):**", ""]
    out += ext_table(rows, "dn_ext", spot, DELTA["normal"])
    out += ["", "**Target probability — pehle breakout par entry, target pehle aaya ya spot ulta OR level tod gaya:**", ""]
    out += target_table(rows, spot, lot)
    out += ["", "**Expected value per trade (spot %, ~0.02% cost minus) — sirf hit-rate nahi, asli kamai:**", ""]
    out += ev_table(rows, spot)
    out += ["", "**Kitni baar break hua (wapas range ke andar close hone ke baad dobara break = naya break):**", ""]
    out += rebreak_table(rows)
    out.append("")
    return out


def expiry_section(key: str, rows: list[dict], weekly_start: str) -> list[str]:
    """Expiry vs non-expiry days, both restricted to the weekly-expiry era, overall and per regime."""
    def stats(rs):
        tr = [r for r in rs if r["first"]]
        return (st.median([r["or_pct"] for r in rs]), st.median([max(r["up_ext"], r["dn_ext"]) for r in rs]),
                pct(sum(1 for r in rs if r["both"]), len(rs)), pct(sum(1 for r in tr if r["full_reverse"]), len(tr)),
                pct(sum(1 for r in tr if r["hits"][0.25] == "hit"), len(tr)),
                pct(sum(1 for r in tr if r["hits"][0.5] == "hit"), len(tr)))
    out = []
    periods = [("poora weekly era", weekly_start, "9999")] + REGIMES[key]
    for label, lo, hi in periods:
        era = [r for r in rows if max(lo, weekly_start) <= r["day"] <= hi]
        e = [r for r in era if r["expiry"]]
        ne = [r for r in era if not r["expiry"]]
        if len(e) < 5 or not ne:
            continue
        a, b = stats(e), stats(ne)
        out.append(f"| {key} — {label} ({e[0]['day']} → {e[-1]['day']}) | {len(e)} / {len(ne)} | "
                   f"{a[0]:.2f}% / {b[0]:.2f}% | {a[1]:.2f}% / {b[1]:.2f}% | {a[2]} / {b[2]} | "
                   f"{a[3]} / {b[3]} | {a[4]} / {b[4]} | {a[5]} / {b[5]} |")
    return out


def main() -> None:
    lots = {"NIFTY": 65, "SENSEX": 20}
    report = ["# ORB market study (10-min opening range, 1-minute Dhan data)", ""]
    exp_rows, cal_notes = [], []
    for key in ("NIFTY", "SENSEX"):
        days = load(key)
        exps, weekly_start = load_calendar(key)
        rule = {d.isoformat() for d in expiry_days(key, set(days))}
        rows = []
        for d in sorted(days):
            r = analyse_day(days[d])
            if r:
                r["day"] = d.isoformat()
                r["expiry"] = r["day"] in exps and r["day"] >= weekly_start
                rows.append(r)
        era = {r["day"] for r in rows if r["day"] >= weekly_start}
        cal = {d for d in exps if d in era}
        wrong_in, wrong_out = sorted((rule & era) - cal), sorted(cal - rule)
        cal_notes.append(f"- {key}: weekly era {weekly_start} se, {len(cal)} expiry din (exchange bhavcopy). "
                         f"Purana weekday rule {len(wrong_in) + len(wrong_out)} din galat tha "
                         f"({len(wrong_in)} galti se expiry maana, {len(wrong_out)} asli expiry miss)."
                         + (f" Example: {', '.join((wrong_in + wrong_out)[:6])}" if wrong_in or wrong_out else ""))
        spot = rows[-1]["close"]
        report.append(f"## {key} (aaj ka spot ~{spot:,.0f}, lot {lots[key]}, option pts = spot pts × delta "
                      f"{DELTA['normal']} normal / {DELTA['expiry']} expiry)")
        report.append("")
        cut5 = (date.fromisoformat(rows[-1]["day"]) - timedelta(days=365 * 5)).isoformat()
        r5 = [r for r in rows if r["day"] >= cut5]
        report += section(key, r5, "last 5 saal", spot, lots[key])
        if rows[0]["day"] < cut5:
            report += section(key, rows, "poora data", spot, lots[key])
        exp_rows += expiry_section(key, rows, weekly_start)
    report += ["## Expiry din vs normal din (expiry / normal, dono sirf weekly-expiry era ke)", "",
               "Expiry dates: NSE/BSE F&O bhavcopy se bana asli exchange calendar (holiday shift sahit).", ""]
    report += cal_notes + ["",
               "| Index — period | Din (expiry / normal) | OR chaudai | Max ek-side move | Dono side break | Full reversal | 0.25% target hit | 0.5% target hit |",
               "|---|---|---|---|---|---|---|---|"] + exp_rows + [""]
    out = HIST / "orb_market_report.md"
    out.write_text("\n".join(report), encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    print("\n".join(report))
    print(f"\nWritten {out}")


if __name__ == "__main__":
    main()
