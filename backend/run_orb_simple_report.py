"""Simple ORB report (Hinglish): 5-min aur 10-min ORB, NIFTY / SENSEX / expiry / mix, aur din me kitni baar breakout.

    .venv\\Scripts\\python.exe run_orb_simple_report.py
    Report: data/history/orb_simple_report.md

Data: har index ke common din = index candles poore AND us din ka option data download ho chuka (maximum history).
Breakout count rule: OR ke bahar gaya = breakout. Candle ka close OR ke andar kam se kam 0.10% (ya OR ke
beech tak, jo pehle ho) wapas aaya to "re-arm"; uske baad phir bahar gaya (upar ya niche) = agla breakout.
Level ke paas ka chhota andar-bahar naya breakout nahi ginta. Entry 15:00 tak hi.
Har breakout ka move = breakout level se max favour move, agle breakout (ya 15:29) tak.
Option = breakout level ka ATM, nearest weekly expiry; gain = option high / entry close - 1.
Read-only analysis; koi order nahi.
"""
from __future__ import annotations

import json
import sys

import numpy as np

from run_orb_5v10_study import (COLS, CUT, HIST, INDICES, M0, OR_LENS, STEP, THRESH, analyse, load_index,
                                load_options, take)
from run_orb_market_study import load_calendar

SHOW = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50, 0.60, 0.75, 1.00]
MAXN = 5                                    # 5+ breakouts ek bucket
REARM = 0.10                                # agla breakout tabhi jab close OR ke andar 0.10% (ya OR ke beech) tak aaye
INFO: dict[str, tuple[int, str, int, str]] = {}


def md(header, rows):
    return ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)] + \
        ["| " + " | ".join(str(c) for c in r) + " |" for r in rows] + [""]


def p(n, d):
    return f"{n / d * 100:.0f}%" if d else "–"


def medp(x):
    x = np.asarray(x, float)
    x = x[~np.isnan(x)]
    return f"{np.median(x):+.0f}%" if len(x) else "–"


def load(k: str):
    """Common din = index candles poore AND option data download ho chuka -> days, index, option, expiry flag."""
    days, a = load_index(k)
    done = set(json.loads((HIST / f"{k}_opt_done.json").read_text()))
    valid = ~np.isnan(a["h"][:, :10]).any(1) & ((~np.isnan(a["c"][:, 10:])).sum(1) >= 200)
    ok = valid & np.isin(days, list(done))
    INFO[k] = (int(valid.sum()), str(days[valid][0]), len(done), str(min(done)))
    days = days[ok]
    a = {f: v[ok] for f, v in a.items()}
    exps, ws = load_calendar(k)
    exp = np.array([d in exps and d >= ws for d in days])
    return days, a, load_options(k, days), exp


def breakouts(k: str, a: dict, opt: dict, n: int) -> list[list[dict]]:
    """Har din ke saare breakouts (1st, 2nd, ...) with side, move % aur option max gain %."""
    hi_all, lo_all = a["h"][:, :n].max(1), a["l"][:, :n].min(1)
    out = []
    for d in range(len(hi_all)):
        hi, lo = hi_all[d], lo_all[d]
        H, L, C = a["h"][d], a["l"][d], a["c"][d]
        buf = min(REARM / 100 * lo, (hi - lo) / 2)
        ev, armed = [], True
        for m in range(n, CUT + 1):
            if np.isnan(H[m]):
                continue
            if armed and (H[m] > hi or L[m] < lo):
                up = H[m] > hi and (L[m] >= lo or H[m] - hi >= lo - L[m])
                ev.append({"m": m, "up": up})
                armed = False
            # Re-arm sirf tab jab close OR ke andar "sach me" wapas aaye (noise wale andar-bahar nahi gine)
            if not armed and lo + buf <= C[m] <= hi - buf:
                armed = True
        for i, e in enumerate(ev):
            end = ev[i + 1]["m"] if i + 1 < len(ev) else COLS
            w = slice(e["m"], end)
            if e["up"]:
                e["move"] = (np.nanmax(H[w]) - hi) / hi * 100
            else:
                e["move"] = (lo - np.nanmin(L[w])) / lo * 100
            side = "CALL" if e["up"] else "PUT"
            lvl = hi if e["up"] else lo
            o = opt[side]
            e["opt"] = np.nan
            if o["k"][d] == np.round(lvl / STEP[k]) * STEP[k]:
                ent = np.nan
                for j in (0, 1, 2):
                    if e["m"] + j < COLS and not np.isnan(o["c"][d, e["m"] + j]):
                        ent = o["c"][d, e["m"] + j]
                        break
                hh = o["h"][d, e["m"] + 1:end]
                if ent > 0 and (~np.isnan(hh)).any():
                    e["opt"] = (np.nanmax(hh) / ent - 1) * 100
        out.append(ev)
    return out


def move_table(r: dict, m: np.ndarray) -> list[str]:
    """Pehla breakout: index kitna gaya -> kitne din -> option kitna bada."""
    tot = int(m.sum())
    br = m & r["broke"]
    rows = []
    for x in SHOW:
        j = int(np.argmin(abs(THRESH - x)))
        hit = br & (r["touch"][:, j] < COLS)
        rows.append([f"{x:.2f}% ya zyada", int(hit.sum()), p(hit.sum(), tot), medp(r["opt_at"][hit, j])])
    head = [f"**{tot} din me se {int(br.sum())} din breakout hua** "
            f"(CE {int((br & r['ce']).sum())} / PE {int((br & ~r['ce']).sum())}). "
            f"0.05% tak bhi nahi gaya: {p((br & (r['touch'][:, 0] >= COLS)).sum(), tot)}", ""]
    return head + md(["Index kitna gaya (breakout ke baad)", "Kitne din", "100 me se", "Option kitna bada (median)"],
                     rows)


def count_table(groups: list[tuple[str, list[list[dict]]]]) -> list[str]:
    rows = []
    for lab, evs in groups:
        c = np.array([len(e) for e in evs])
        d = len(c)
        rows.append([lab, d, f"{c.mean():.1f}"] + [p((c == i).sum(), d) for i in range(MAXN)] + [p((c >= MAXN).sum(), d)])
    return md(["Group", "Din", "Avg breakout/din"] + [f"{i} baar" for i in range(MAXN)] + [f"{MAXN}+ baar"], rows)


def nth_table(evs: list[list[dict]]) -> list[str]:
    """1st, 2nd, 3rd... breakout: kitne din aaya, UP aur DOWN alag — kitni baar, index kitna gaya, option kitna bada."""
    d = len(evs)
    rows = []
    for i in range(MAXN):
        es = [e[i] for e in evs if len(e) > i]
        if not es:
            continue
        row = [f"{i + 1}{'st' if i == 0 else 'nd' if i == 1 else 'rd' if i == 2 else 'th'}", f"{len(es)} ({p(len(es), d)})"]
        for up in (True, False):
            s = [e for e in es if e["up"] == up]
            mv = np.array([e["move"] for e in s])
            row += [len(s), f"{np.median(mv):.2f}%" if len(s) else "–", p((mv >= .2).sum(), len(s)),
                    medp([e["opt"] for e in s])]
        rows.append(row)
    return md(["Breakout no.", "Kitne din aaya (100 me se)", "UP din", "UP index move", "UP 0.20%+ gaya",
               "UP Call option bada", "DOWN din", "DOWN index move", "DOWN 0.20%+ gaya", "DOWN Put option bada"], rows)


def pattern_table(groups: list[tuple[str, list[list[dict]]]], top: int = 8) -> list[str]:
    seqs = {lab: ["→".join("UP" if x["up"] else "DOWN" for x in e) or "koi nahi" for e in evs] for lab, evs in groups}
    allc: dict[str, int] = {}
    for s in seqs.values():
        for x in s:
            allc[x] = allc.get(x, 0) + 1
    keys = sorted(allc, key=lambda x: -allc[x])[:top]
    rows = [[k] + [p(sum(x == k for x in s), len(s)) for s in seqs.values()] for k in keys]
    return md(["Din ka pattern"] + list(seqs), rows)


def main() -> None:
    data = {k: load(k) for k in INDICES}
    rep = ["# ORB simple report — NIFTY + SENSEX", "",
           "| Index | Index data din | Option data din | **Common din (100%)** | Normal din | Expiry din |",
           "|---|---|---|---|---|---|"]
    for k, (days, _, _, exp) in data.items():
        iv, i0, od, o0 = INFO[k]
        rep.append(f"| {k} | {iv} ({i0} se) | {od} ({o0} se) | **{len(days)}** ({days[0]} → {days[-1]}) | "
                   f"{int((~exp).sum())} | {int(exp.sum())} |")
    rep += ["", "Option = breakout level ka ATM weekly option. \"Option kitna bada\" = jab index us % par pahucha "
            "us minute ka option high vs entry. Breakout = OR high ke upar (CE) ya OR low ke niche (PE).",
            "Breakout ginti: agla breakout tabhi gina jab price OR ke andar 0.10% (ya OR ke beech tak) wapas aaya.", ""]

    for n in OR_LENS:
        res = {k: analyse(k, d[1], d[2], n, d[3]) for k, d in data.items()}
        rep += [f"# {n}-MIN ORB", ""]
        groups = []
        for k in INDICES:
            exp = data[k][3]
            for lab, m in ((f"{k} normal din", ~exp), (f"{k} expiry din", exp)):
                rep += [f"## {n}-min · {lab}", "", *move_table(res[k], m)]
        # Mix: NIFTY + SENSEX saare din ek saath
        mix = {f: np.concatenate([res[k][f] for k in INDICES]) for f in ("broke", "ce", "touch", "opt_at")}
        rep += [f"## {n}-min · MIX (NIFTY + SENSEX saare din)", "", *move_table(mix, np.ones(len(mix["broke"]), bool))]

        ev = {k: breakouts(k, data[k][1], data[k][2], n) for k in INDICES}
        for k in INDICES:
            exp = data[k][3]
            groups += [(f"{k} normal", [e for e, x in zip(ev[k], exp) if not x]),
                       (f"{k} expiry", [e for e, x in zip(ev[k], exp) if x])]
        groups.append(("MIX", ev["NIFTY"] + ev["SENSEX"]))
        rep += [f"## {n}-min · din me kitni baar breakout", "", *count_table(groups),
                f"### {n}-min · din ka pattern (pehle kidhar, phir kidhar)", "", *pattern_table(groups)]
        for lab, evs in groups:
            rep += [f"### {n}-min · {lab}: 1st, 2nd, 3rd... breakout kitna chala", "", *nth_table(evs)]

    out = HIST / "orb_simple_report.md"
    out.write_text("\n".join(rep), encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    print("\n".join(rep))
    print(f"\nWritten {out}")


if __name__ == "__main__":
    main()
