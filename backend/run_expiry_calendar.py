"""Historical index-option expiry calendar from the exchanges' own F&O bhavcopies.

    .venv\\Scripts\\python.exe run_expiry_calendar.py      # writes data/history/expiry_calendar.json

Every bhavcopy lists all live option expiries, so one file every ~10 trading days covers
every weekly/monthly expiry; any week left without an expiry is re-sampled from that week.
NIFTY = NSE (OPTIDX/IDO NIFTY), SENSEX = BSE (IO/IDO SENSEX, not SENSEX50).
Samples are cached, so re-runs only download new trading days.
"""
from __future__ import annotations

import csv
import gzip
import io
import json
import time
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

HIST = Path(__file__).resolve().parent / "data" / "history"
OUT = HIST / "expiry_calendar.json"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
START = {"NIFTY": date(2018, 11, 1), "SENSEX": date(2021, 9, 1)}
STEP = 10
LIQ_SHARE = 0.10     # same-week expiry with <10% of its sibling's volume = long-dated/thin contract


def _get(url: str, referer: str) -> bytes | None:
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": referer})
            return urllib.request.urlopen(req, timeout=30).read()
        except Exception:  # noqa: BLE001
            time.sleep(1.5 * (attempt + 1))
    return None


def _rows(blob: bytes) -> list[dict]:
    if blob[:2] == b"PK":
        z = zipfile.ZipFile(io.BytesIO(blob))
        blob = z.read(z.namelist()[0])
    text = blob.decode("utf-8", "replace")
    if text.lstrip().startswith("<"):
        return []
    try:
        return list(csv.DictReader(text.splitlines()))
    except csv.Error:
        return []


def _parse(value: str) -> date | None:
    value = (value or "").strip()
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d %b %Y"):
        try:
            return datetime.strptime(value.title(), fmt).date()
        except ValueError:
            continue
    return None


def _add(out: dict[date, float], expiry: str | None, volume: str | None) -> None:
    d = _parse(expiry or "")
    if d:
        try:
            v = float((volume or "0").replace(",", "") or 0)
        except ValueError:
            v = 0.0
        out[d] = out.get(d, 0.0) + v


def _udiff(rows: list[dict], symbol: str) -> dict[date, float]:
    out: dict[date, float] = {}
    for r in rows:
        if r.get("FinInstrmTp") == "IDO" and r.get("TckrSymb") == symbol and r.get("OptnTp") in ("CE", "PE"):
            _add(out, r.get("FininstrmActlXpryDt") or r.get("XpryDt"), r.get("TtlTradgVol"))
    return out


def nifty_expiries(d: date) -> dict[date, float] | None:
    """{expiry: option volume traded that day} for NIFTY index options, or None if no file."""
    ref = "https://www.nseindia.com/"
    new = f"https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"
    mon = d.strftime("%b").upper()
    old = (f"https://nsearchives.nseindia.com/content/historical/DERIVATIVES/{d:%Y}/{mon}/"
           f"fo{d:%d}{mon}{d:%Y}bhav.csv.zip")
    for url in ((new, old) if d >= date(2024, 7, 8) else (old, new)):
        blob = _get(url, ref)
        rows = _rows(blob) if blob else []
        if not rows:
            continue
        if "FinInstrmTp" in rows[0]:
            return _udiff(rows, "NIFTY")
        out: dict[date, float] = {}
        for r in rows:
            if r.get("INSTRUMENT") == "OPTIDX" and r.get("SYMBOL") == "NIFTY":
                _add(out, r.get("EXPIRY_DT"), r.get("CONTRACTS"))
        return out
    return None


def sensex_expiries(d: date) -> dict[date, float] | None:
    """{expiry: option volume traded that day} for SENSEX (not SENSEX50) options, or None if no file."""
    ref = "https://www.bseindia.com/"
    new = f"https://www.bseindia.com/download/Bhavcopy/Derivative/BhavCopy_BSE_FO_0_0_0_{d:%Y%m%d}_F_0000.CSV"
    old = f"https://www.bseindia.com/download/Bhavcopy/Derivative/bhavcopy{d:%d-%m-%y}.zip"
    for url in (new, old):
        blob = _get(url, ref)
        rows = _rows(blob) if blob else []
        if not rows:
            continue
        if "FinInstrmTp" in rows[0]:
            return _udiff(rows, "SENSEX")
        out: dict[date, float] = {}
        for r in rows:
            sym = r.get("Symbol") or ""
            if r.get("Contract Type") == "IO" and sym.startswith("SENSEX") and not sym.startswith("SENSEX50"):
                _add(out, r.get("Expiry"), r.get("Contracts Traded"))
        return out
    return None


FETCH = {"NIFTY": nifty_expiries, "SENSEX": sensex_expiries}


def trading_days(key: str) -> list[date]:
    days = set()
    with gzip.open(HIST / f"{key}_1m.csv.gz", "rt", newline="") as f:
        r = csv.reader(f)
        next(r)
        for row in r:
            days.add(row[0][:10])
    return sorted(date.fromisoformat(x) for x in days)


def build(key: str, cache: dict) -> dict:
    samples: dict[str, dict[str, float]] = cache.get(key, {}).get("samples", {})
    if any(isinstance(v, list) for v in samples.values()):
        samples = {}                                   # old cache without volumes
    missing: list[str] = cache.get(key, {}).get("missing", [])
    days = [d for d in trading_days(key) if d >= START[key]]
    trading = set(days)

    def run(batch: list[date]) -> None:
        todo = [d for d in batch if d.isoformat() not in samples and d.isoformat() not in missing]
        with ThreadPoolExecutor(4) as pool:
            for d, res in zip(todo, pool.map(FETCH[key], todo)):
                if res is None:
                    missing.append(d.isoformat())
                else:
                    samples[d.isoformat()] = {x.isoformat(): v for x, v in sorted(res.items())}
        print(f"{key}: fetched {len(todo)} file(s), samples={len(samples)}, unavailable={len(missing)}")

    run(days[::STEP] + days[-1:])
    for _ in range(3):
        exp = {e for v in samples.values() for e in v}
        weeks_with_exp = {date.fromisoformat(e).isocalendar()[:2] for e in exp}
        first = min((date.fromisoformat(e) for e in exp), default=None)
        # Old BSE files list only traded contracts, so sparse samples can hide weeks: fill from before the first expiry seen.
        gaps = [d for d in days if first and d >= first - timedelta(days=21)
                and d.isocalendar()[:2] not in weeks_with_exp]
        week_heads = sorted({min(x for x in gaps if x.isocalendar()[:2] == g.isocalendar()[:2]) for g in gaps})
        if not week_heads:
            break
        run(week_heads)

    lo, hi = START[key].isoformat(), days[-1].isoformat()
    listed = sorted({e for v in samples.values() for e in v if lo <= e <= hi})
    by_week: dict[tuple, list[str]] = {}
    for e in listed:
        by_week.setdefault(date.fromisoformat(e).isocalendar()[:2], []).append(e)
    liquid = []
    for group in by_week.values():
        if len(group) == 1:
            liquid += group
            continue
        # Several expiries in one week. A date that a later file (still before it) no longer lists, while a
        # sibling date is listed, was re-dated by the exchange (e.g. Thu -> Tue in Sep-2025): drop it.
        live = []
        for e in group:
            last_seen = max((s for s, vols in samples.items() if e in vols), default="")
            redated = any(last_seen < s <= e and e not in vols and any(g in vols for g in group if g != e)
                          for s, vols in samples.items())
            if not redated:
                live.append(e)
        group = live or group
        if len(group) == 1:
            liquid += group
            continue
        # Still several: compare them on the SAME sample day (same distance to expiry).
        best = {e: 0.0 for e in group}
        for vols in samples.values():
            top = max((vols.get(e, 0.0) for e in group), default=0.0)
            if top > 0 and sum(1 for e in group if e in vols) > 1:
                for e in group:
                    best[e] = max(best[e], vols.get(e, 0.0) / top)
        if not any(best.values()):
            liquid += group
            continue
        liquid += [e for e in group if best[e] >= LIQ_SHARE]
    liquid.sort()
    # A listed date that later became an exchange holiday (e.g. NIFTY 29-Jun-2023, SENSEX 15-Jan-2026) expired
    # on the previous trading day of the same week; old-format files keep showing the nominal date.
    shifted = {}
    for e in liquid:
        d = date.fromisoformat(e)
        if d in trading:
            continue
        prev = [t for t in days if t < d and t.isocalendar()[:2] == d.isocalendar()[:2]]
        if prev and not any(x != e and date.fromisoformat(x).isocalendar()[:2] == d.isocalendar()[:2]
                            and date.fromisoformat(x) in trading for x in liquid):
            shifted[e] = max(prev).isoformat()
    liquid = sorted({shifted.get(e, e) for e in liquid})
    ws = _weekly_start([date.fromisoformat(e) for e in liquid], days)
    weeks_liq = {date.fromisoformat(e).isocalendar()[:2] for e in liquid}
    gap_weeks = sorted({f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}" for d in days
                        if ws and d >= ws and d.isocalendar()[:2] not in weeks_liq})
    return {"samples": samples, "missing": sorted(set(missing)), "listed_expiries": listed,
            "expiries": [e for e in liquid if date.fromisoformat(e) in trading],
            "holiday_shifted": shifted,
            "thin_expiries": [e for e in listed if e not in liquid and e not in shifted],
            "not_in_candle_cache": [e for e in liquid if date.fromisoformat(e) not in trading],
            "weekly_start": ws.isoformat() if ws else None, "gap_weeks": gap_weeks}


def _weekly_start(liquid: list[date], days: list[date]) -> date | None:
    """First liquid expiry after which every trading week (for at least 8 weeks) has a liquid expiry."""
    weeks = {d.isocalendar()[:2] for d in liquid}
    trade_weeks = sorted({d.isocalendar()[:2] for d in days})
    for e in sorted(liquid):
        i = trade_weeks.index(e.isocalendar()[:2]) if e.isocalendar()[:2] in trade_weeks else None
        if i is not None and all(w in weeks for w in trade_weeks[i:i + 8]):
            return e
    return None


def main() -> None:
    cache = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    out = {"source": "NSE/BSE F&O bhavcopy archives (actual expiry dates)", "built": date.today().isoformat()}
    for key in ("NIFTY", "SENSEX"):
        out[key] = build(key, cache)
        k = out[key]
        print(f"{key}: {len(k['expiries'])} expiries, weekly since {k['weekly_start']}, gap weeks {k['gap_weeks']}")
        OUT.write_text(json.dumps({**cache, **out}, indent=1), encoding="utf-8")
    OUT.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(f"Written {OUT}")


if __name__ == "__main__":
    main()
