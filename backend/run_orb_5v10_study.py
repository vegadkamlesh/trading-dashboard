"""5-min vs 10-min ORB study: index move % buckets mapped to real option-premium expansion.

    .venv\\Scripts\\python.exe run_orb_5v10_study.py             # latest 450 common din (default)
    .venv\\Scripts\\python.exe run_orb_5v10_study.py --days 0    # saare common din
    Report: data/history/orb_5v10_report.md

Pehle ye chalao: run_history_cache.py, run_expiry_calendar.py, run_option_cache.py (Master Dhan API).
Needs numpy + pandas (requirements-research.txt).

Data alignment:
  * Index 1-min candles (NIFTY, SENSEX) aur option premium cache, dono ke common trading days hi use hote hain.
  * Common day = dono index ke poore candles (09:15-09:24 + kam se kam 200 post-OR candles) AND dono ke
    option-cache range ke andar AND jis din breakout hua us din ka option data download ho chuka.
  * In common days me se sabse naye SAMPLE_DAYS (default 450) din kaate jaate hain; yahi 100% sample hai.
    Common din 450 se kam hon to jitne hain utne use hote hain aur report me warning aati hai.
Option data: Dhan expired-options 1-min data, sirf wo strike jo live algo kharidta hai
(10-min OR level ka ATM, nearest weekly expiry). 5-min ORB ka ATM strike alag ho to us trade ka premium
nahi milta; aise trades index stats me gine jaate hain, premium stats me nahi (coverage % report me hai).
Read-only analysis; koi order nahi.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from run_orb_market_study import load_calendar

ROOT = Path(__file__).resolve().parent
HIST = ROOT / "data" / "history"
M0, COLS = 9 * 60 + 15, 375                 # column 0 = 09:15, column 374 = 15:29
CUT = 15 * 60 - M0                          # entry 15:00 tak hi (live algo jaisa)
OR_LENS = (5, 10)
THRESH = np.round(np.arange(1, 21) * 0.05, 2)   # 0.05% ... 1.00%
STEP = {"NIFTY": 50, "SENSEX": 100}
INDICES = ("NIFTY", "SENSEX")
CIDX = np.arange(COLS)
SAMPLE_DAYS = 450                           # common din ka fixed baseline (0 = saare common din)


# ---------------------------------------------------------------- chhote helpers
def first_true(mask: np.ndarray) -> np.ndarray:
    """Har row me pehla True column; koi True nahi to COLS (matlab 'kabhi nahi')."""
    return np.where(mask.any(1), mask.argmax(1), COLS)


def take(a: np.ndarray, col: np.ndarray) -> np.ndarray:
    """Har row ka ek column uthao; col >= COLS ho to NaN."""
    v = a[np.arange(len(a)), np.clip(col, 0, COLS - 1)]
    return np.where(col < COLS, v, np.nan)


def row_max(a: np.ndarray) -> np.ndarray:
    """NaN ignore karke row max; poori row NaN ho to NaN (warning ke bina)."""
    m = np.where(np.isnan(a), -np.inf, a).max(1)
    return np.where(np.isinf(m), np.nan, m)


def last_valid(a: np.ndarray, start: np.ndarray) -> np.ndarray:
    """start column se aage ka aakhri non-NaN value (15:29 wala close)."""
    ok = ~np.isnan(a) & (CIDX[None, :] >= start[:, None])
    idx = COLS - 1 - ok[:, ::-1].argmax(1)
    return np.where(ok.any(1), a[np.arange(len(a)), idx], np.nan)


def pct(num, den) -> str:
    return f"{num / den * 100:.1f}%" if den else "–"


def med(x) -> str:
    x = np.asarray(x, float)
    x = x[~np.isnan(x)]
    return f"{np.median(x):+.1f}%" if len(x) else "–"


def avg(x) -> str:
    x = np.asarray(x, float)
    x = x[~np.isnan(x)]
    return f"{np.mean(x):+.1f}%" if len(x) else "–"


def hhmm(col: float) -> str:
    m = int(col) + M0
    return f"{m // 60:02d}:{m % 60:02d}"


def md(header: list[str], rows: list[list[str]]) -> list[str]:
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    return out + ["| " + " | ".join(str(c) for c in r) + " |" for r in rows] + [""]


# ---------------------------------------------------------------- data load (vectorized)
def load_index(key: str) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    """1-min index candles -> (days, {'h','l','c'}: array[din, minute])."""
    df = pd.read_csv(HIST / f"{key}_1m.csv.gz")
    df.columns = ["ts", "o", "h", "l", "c"]
    df["day"] = df["ts"].str[:10]
    df["col"] = df["ts"].str[11:13].astype(int) * 60 + df["ts"].str[14:16].astype(int) - M0
    df = df[(df["col"] >= 0) & (df["col"] < COLS)]
    days = np.array(sorted(df["day"].unique()))
    di = np.searchsorted(days, df["day"].to_numpy())
    arr = {}
    for f in ("h", "l", "c"):
        a = np.full((len(days), COLS), np.nan)
        a[di, df["col"].to_numpy()] = df[f].to_numpy(float)
        arr[f] = a
    return days, arr


def load_options(key: str, days: np.ndarray) -> dict[str, dict[str, np.ndarray]]:
    """Option premium cache -> side ('CALL'/'PUT') -> {'h','c': array[din, minute], 'k': strike[din]}."""
    df = pd.read_csv(HIST / f"{key}_opt_1m.csv.gz", usecols=["day", "side", "strike", "minute", "high", "close"])
    df = df[df["day"].isin(set(days))]
    df["col"] = df["minute"] - M0
    df = df[(df["col"] >= 0) & (df["col"] < COLS)]
    out = {}
    for side in ("CALL", "PUT"):
        s = df[df["side"] == side]
        di = np.searchsorted(days, s["day"].to_numpy())
        h, c, k = (np.full((len(days), COLS), np.nan), np.full((len(days), COLS), np.nan),
                   np.full(len(days), np.nan))
        h[di, s["col"].to_numpy()] = s["high"].to_numpy(float)
        c[di, s["col"].to_numpy()] = s["close"].to_numpy(float)
        k[di] = s["strike"].to_numpy(float)
        out[side] = {"h": h, "c": c, "k": k}
    return out


def or_levels(a: dict, n: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """n-minute OR high/low aur pehla breakout column (15:00 tak)."""
    hi, lo = a["h"][:, :n].max(1), a["l"][:, :n].min(1)
    win = (CIDX >= n) & (CIDX <= CUT)
    fu = first_true((a["h"] > hi[:, None]) & win)
    fd = first_true((a["l"] < lo[:, None]) & win)
    return hi, lo, np.minimum(fu, fd)


def align() -> tuple[np.ndarray, dict, dict, list[list[str]]]:
    """Saare sources ko common trading days par kaat do (yahi 100% sample hai)."""
    raw, keep, info = {}, {}, []
    for k in INDICES:
        days, a = load_index(k)
        done = set(json.loads((HIST / f"{k}_opt_done.json").read_text()))
        # Valid index din: OR ke 10 candles + kam se kam 200 post-OR candles
        valid = ~np.isnan(a["h"][:, :10]).any(1) & ((~np.isnan(a["c"][:, 10:])).sum(1) >= 200)
        # 10-min breakout hua par option data download nahi hua -> drop (adhura din)
        _, _, b10 = or_levels(a, 10)
        has_opt = np.isin(days, list(done))
        in_range = (days >= min(done)) & (days <= max(done))
        ok = valid & in_range & ((b10 >= COLS) | has_opt)
        raw[k], keep[k] = (days, a), set(days[ok])
        info.append([k, f"{int(valid.sum())} ({days[valid][0]} → {days[valid][-1]})",
                     f"{len(done)} ({min(done)} → {max(done)})", f"{int(ok.sum())}"])
    common = np.array(sorted(set.intersection(*keep.values())))
    if not len(common):
        sys.exit("Koi common din nahi mila — pehle cache scripts chalao.")
    info.append(["Common (dono index)", f"{len(common)} ({common[0]} → {common[-1]})", "", ""])
    # Truncation rule: sabse naye SAMPLE_DAYS common din hi rakho, baaki sab sources isi par kaate jaate hain
    if SAMPLE_DAYS and len(common) > SAMPLE_DAYS:
        common = common[-SAMPLE_DAYS:]
    info.append(["**Sample (100%)**", f"**{len(common)}** ({common[0]} → {common[-1]})", "", ""])
    idx, opt = {}, {}
    for k, (days, a) in raw.items():
        pos = np.searchsorted(days, common)
        idx[k] = {f: v[pos] for f, v in a.items()}
        opt[k] = load_options(k, common)
    return common, idx, opt, info


# ---------------------------------------------------------------- ORB + option mapping
def analyse(key: str, a: dict, opt: dict, n: int, expiry: np.ndarray) -> dict:
    """Ek index + ek OR length: har din ka first-trigger breakout, index move aur premium move."""
    hi, lo, b = or_levels(a, n)
    broke = b < COLS
    # First-trigger rule: jo side pehle tooti wahi trade; same minute me dono toote to jo zyada toota
    up_first = first_true((a["h"] > hi[:, None]) & (CIDX >= n) & (CIDX <= CUT)) == b
    dn_first = first_true((a["l"] < lo[:, None]) & (CIDX >= n) & (CIDX <= CUT)) == b
    tie = up_first & dn_first
    ce = np.where(tie, (take(a["h"], b) - hi) >= (lo - take(a["l"], b)), up_first) & broke
    base = np.where(ce, hi, lo)

    # Favour move % har minute (CE: high kitna upar, PE: low kitna niche), breakout minute se aage
    after = CIDX[None, :] >= b[:, None]
    fav = np.where(ce[:, None], (a["h"] - hi[:, None]) / hi[:, None], (lo[:, None] - a["l"]) / lo[:, None]) * 100
    fav = np.where(after & ~np.isnan(fav) & broke[:, None], fav, -np.inf)
    peak = np.where(broke, fav.max(1), np.nan)
    # Ulta OR level bhi toota? (CE ke baad OR low, PE ke baad OR high)
    opp = np.where(ce[:, None], a["l"] < lo[:, None], a["h"] > hi[:, None]) & (CIDX[None, :] > b[:, None])
    fo = np.where(broke, first_true(opp), COLS)
    touch = np.stack([first_true(fav >= x - 1e-9) for x in THRESH], 1)     # [din, bucket]
    close = last_valid(a["c"], np.zeros(len(b), int))
    eod = np.where(ce, (close - hi) / hi, (lo - close) / lo) * 100

    # Option: ATM strike of breakout level; cache wala strike same ho tabhi premium valid
    k_atm = np.round(base / STEP[key]) * STEP[key]
    oh = np.where(ce[:, None], opt["CALL"]["h"], opt["PUT"]["h"])
    oc = np.where(ce[:, None], opt["CALL"]["c"], opt["PUT"]["c"])
    ok = broke & (np.where(ce, opt["CALL"]["k"], opt["PUT"]["k"]) == k_atm)
    entry = take(oc, b)
    for d in (1, 2):                          # breakout minute ka candle missing ho to agla 1-2 minute
        entry = np.where(np.isnan(entry), take(oc, b + d), entry)
    entry = np.where(ok & (entry > 0), entry, np.nan)
    later = CIDX[None, :] > b[:, None]
    opt_max = (row_max(np.where(later, oh, np.nan)) / entry - 1) * 100
    opt_eod = (last_valid(oc, b) / entry - 1) * 100
    # Index X% par pahucha us minute ka option high
    opt_at = np.stack([(take(oh, touch[:, j]) / entry - 1) * 100 for j in range(len(THRESH))], 1)
    return {"key": key, "n": n, "days": len(b), "broke": broke, "ce": ce, "b": b, "peak": peak, "fo": fo,
            "touch": touch, "eod": eod, "or_pct": (hi - lo) / lo * 100, "expiry": expiry,
            "entry": entry, "opt_max": opt_max, "opt_eod": opt_eod, "opt_at": opt_at}


# ---------------------------------------------------------------- tables
def success(r: dict, x: float, m: np.ndarray) -> np.ndarray:
    """Target x% pehle aaya, ulta OR level baad me (ya kabhi nahi)."""
    j = int(np.argmin(abs(THRESH - x)))
    return (r["touch"][:, j] < r["fo"])[m]


METRIC_LABELS = ["Sample din", "Breakout hua", "CE pehle / PE pehle", "Median breakout time",
                 "OR chaudai (median)", "Break ke baad max move (median)", "Ulta OR level bhi toota",
                 "0.10% target pehle (ulta OR se pehle)", "0.20% target pehle", "0.30% target pehle",
                 "15:29 par favour me band", "Option premium data mila", "Entry premium (median)",
                 "Premium max gain (median)", "Premium ≥ +20% gaya", "Premium ≥ +50% gaya", "Premium ≥ +100% gaya",
                 "15:29 tak hold, premium return (avg / median)"]


def metrics(r: dict, m: np.ndarray) -> list[str]:
    br = m & r["broke"]
    nb = int(br.sum())
    o = br & ~np.isnan(r["entry"])
    no = int(o.sum())
    if not nb:
        return [str(int(m.sum()))] + ["–"] * (len(METRIC_LABELS) - 1)
    mx = r["opt_max"][o]
    return [str(int(m.sum())), pct(nb, m.sum()),
            f"{pct((br & r['ce']).sum(), nb)} / {pct((br & ~r['ce']).sum(), nb)}",
            hhmm(np.median(r["b"][br])), f"{np.median(r['or_pct'][m]):.2f}%", f"{np.median(r['peak'][br]):.2f}%",
            pct((r["fo"][br] < COLS).sum(), nb),
            pct(success(r, .1, br).sum(), nb), pct(success(r, .2, br).sum(), nb), pct(success(r, .3, br).sum(), nb),
            pct((r["eod"][br] > 0).sum(), nb), pct(no, nb),
            f"₹{np.median(r['entry'][o]):.1f}" if no else "–", med(mx),
            pct((mx >= 20).sum(), no), pct((mx >= 50).sum(), no), pct((mx >= 100).sum(), no),
            f"{avg(r['opt_eod'][o])} / {med(r['opt_eod'][o])}"]


def compare(cols: list[tuple[str, dict, np.ndarray]]) -> list[str]:
    vals = [metrics(r, m) for _, r, m in cols]
    return md(["Metric"] + [c[0] for c in cols], [[lab] + [v[i] for v in vals] for i, lab in enumerate(METRIC_LABELS)])


def bucket_table(rs: list[dict]) -> list[str]:
    """Index move bucket vs frequency (100 din me se) vs option premium expansion. rs ko pool karta hai."""
    total = sum(r["days"] for r in rs)
    cat = lambda f: np.concatenate([f(r) for r in rs])
    peak, broke, exp = cat(lambda r: r["peak"]), cat(lambda r: r["broke"]), cat(lambda r: r["expiry"])
    touch, opt_at = cat(lambda r: r["touch"]), cat(lambda r: r["opt_at"])
    rows = []
    for j, x in enumerate(THRESH):
        hit = broke & (touch[:, j] < COLS)
        hi_edge = THRESH[j + 1] if j + 1 < len(THRESH) else np.inf
        stop = broke & (peak >= x - 1e-9) & (peak < hi_edge - 1e-9)
        g = opt_at[:, j]
        rows.append([f"{x:.2f}%" + ("+" if j == len(THRESH) - 1 else f" – {hi_edge:.2f}%"),
                     pct(hit.sum(), total), pct(stop.sum(), total),
                     f"{avg(g[hit])} / {med(g[hit])}", med(g[hit & ~exp]), med(g[hit & exp]),
                     str(int((hit & ~np.isnan(g)).sum()))])
    out = md(["Index move (breakout level se)", "Kam se kam yahan tak gaya (100 din me se)",
              "Max move isi bucket me ruka (100 din me se)", "Option gain jab index yahan pahucha (avg / median)",
              "Normal din (median)", "Expiry din (median)", "Premium sample"], rows)
    nob = (~broke).sum()
    small = (broke & (peak < THRESH[0] - 1e-9)).sum()
    return out + [f"Baaki: breakout hi nahi hua {pct(nob, total)}, break hua par 0.05% tak nahi gaya {pct(small, total)} "
                  f"(teesra column + ye dono = 100%).", ""]


def first_trigger(res: dict, n: int, cats: dict[str, np.ndarray]) -> list[str]:
    """Common din par kaunsa index pehle toota (live algo jaisa: pehla break hi trade, dusra lock)."""
    rn, rs = res[("NIFTY", n)], res[("SENSEX", n)]
    bn, bs = rn["b"], rs["b"]
    nifty_first = (bn < COLS) & (bn <= bs)            # same minute -> NIFTY (algo ka order)
    sensex_first = (bs < COLS) & (bs < bn)
    rows = []
    for lab, m in cats.items():
        d = int(m.sum())
        if not d:
            continue
        tak = lambda r, w: (success(r, .2, m & w).sum(), r["opt_max"][m & w])
        sn, on = tak(rn, nifty_first)
        ss, os_ = tak(rs, sensex_first)
        trades = (m & (nifty_first | sensex_first)).sum()
        rows.append([lab, d, pct((m & nifty_first & (bn < bs)).sum(), d), pct((m & sensex_first).sum(), d),
                     pct((m & (bn == bs) & (bn < COLS)).sum(), d), pct((m & (bn >= COLS) & (bs >= COLS)).sum(), d),
                     pct(sn + ss, trades), med(np.concatenate([on, os_]))])
    return md(["Din ka type", "Din", "NIFTY pehle", "SENSEX pehle", "Same minute", "Kisi ne nahi toda",
               "Pehle wale trade me 0.20% target pehle", "Pehle wale trade ka premium max gain (median)"], rows)


# ---------------------------------------------------------------- main
def main() -> None:
    global SAMPLE_DAYS
    if "--days" in sys.argv:
        SAMPLE_DAYS = int(sys.argv[sys.argv.index("--days") + 1])
    common, idx, opt, info = align()
    short = (f"⚠️ Sirf {len(common)} common din mile, {SAMPLE_DAYS} se kam — jitne hain utne use kiye."
             if SAMPLE_DAYS and len(common) < SAMPLE_DAYS else "")
    exp = {}
    for k in INDICES:
        exps, ws = load_calendar(k)
        exp[k] = np.array([d in exps and d >= ws for d in common])
    res = {(k, n): analyse(k, idx[k], opt[k], n, exp[k]) for k in INDICES for n in OR_LENS}
    alld = np.ones(len(common), bool)

    rep = [f"# 5-min vs 10-min ORB study — NIFTY + SENSEX ({common[0]} → {common[-1]})", "",
           "## Data alignment", "",
           *md(["Index", "Index din (valid)", "Option-cache din (range)", "Is index ke liye usable din"], info),
           f"**Sample = {len(common)} common trading din = 100%** (dono index ke candles + option data dono "
           f"maujood; sabse naye din liye).", short,
           "Option gain = (option ka high jab index us % par pahucha − entry) / entry. Entry = breakout minute "
           "ka option close (live algo jaisa), strike = breakout level ka ATM, nearest weekly expiry.", ""]
    for sec, k in ((1, "NIFTY"), (2, "SENSEX")):
        r5, r10 = res[(k, 5)], res[(k, 10)]
        rep += [f"## SECTION {sec}: {k} ({len(common)} din)", "", "### 5-min vs 10-min ORB", "",
                *compare([("5-min ORB", r5, alld), ("10-min ORB", r10, alld)]),
                "### Expiry din vs Normal din", "",
                *compare([("5-min Normal", r5, ~exp[k]), ("5-min Expiry", r5, exp[k]),
                          ("10-min Normal", r10, ~exp[k]), ("10-min Expiry", r10, exp[k])])]
        for n in OR_LENS:
            rep += [f"### {k} {n}-min ORB: index move bucket vs frequency vs option premium", "",
                    *bucket_table([res[(k, n)]])]

    rep += ["## SECTION 3: NIFTY + SENSEX combined", "", "### Side-by-side", ""]
    rep += compare([(f"{k} {n}-min", res[(k, n)], alld) for k in INDICES for n in OR_LENS])
    cats = {"Sab din": alld, "Koi expiry nahi": ~exp["NIFTY"] & ~exp["SENSEX"],
            "Sirf NIFTY expiry": exp["NIFTY"] & ~exp["SENSEX"], "Sirf SENSEX expiry": exp["SENSEX"] & ~exp["NIFTY"],
            "Dono ki expiry same din": exp["NIFTY"] & exp["SENSEX"]}
    for n in OR_LENS:
        rep += [f"### {n}-min ORB: kaunsa index pehle toota", "", *first_trigger(res, n, cats)]
    for n in OR_LENS:
        rep += [f"### Unified bucket table, {n}-min ORB (NIFTY + SENSEX pooled, {2 * len(common)} index-din = 100%)", "",
                *bucket_table([res[("NIFTY", n)], res[("SENSEX", n)]])]

    out = HIST / "orb_5v10_report.md"
    out.write_text("\n".join(rep), encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    print("\n".join(rep))
    print(f"\nWritten {out}")


if __name__ == "__main__":
    main()
