"""1-minute premium cache of the exact option the ORB algo would trade (Dhan /charts/rollingoption).

    .venv\\Scripts\\python.exe run_option_cache.py           # back-fill, then append new days
    .venv\\Scripts\\python.exe run_option_cache.py NIFTY

For every day and side whose OR level breaks (spot cache, run_history_cache.py first), the live
algo buys the nearest-expiry ATM strike of the breakout level: K = round(level / step) * step.
Dhan stores expired options per minute relative to spot (ATM, ATM+1 ...), so this fetches the
offsets K can occupy after the break and keeps only the bars whose strike == K.
The API accepts at most two calendar-consecutive days per call (fromDate and toDate inclusive).
Output: data/history/<INDEX>_opt_1m.csv.gz (day,side,strike,hhmm,open,high,low,close,iv,spot)
plus <INDEX>_opt_done.json (days finished). Read-only market data; never places orders.
"""
from __future__ import annotations

import csv
import gzip
import json
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

from run_history_cache import env
from run_orb_market_study import load

ROOT = Path(__file__).resolve().parent
HIST = ROOT / "data" / "history"
IST = timezone(timedelta(hours=5, minutes=30))
URL = "https://api.dhan.co/v2/charts/rollingoption"
CFG = {  # segment, underlying id, strike step, first day worth fetching (weekly options liquid)
    "NIFTY": ("NSE_FNO", "13", 50, date(2021, 1, 1)),
    "SENSEX": ("BSE_FNO", "51", 100, date(2023, 5, 15)),
}
OR_END, ENTRY_CUTOFF, EOD = 9 * 60 + 24, 15 * 60, 15 * 60 + 29
MAX_OFF = 10
FIELDS = ["open", "high", "low", "close", "iv", "strike", "spot"]
_lock = threading.Lock()
_last = [0.0]
MIN_GAP = 0.22          # ~4.5 requests/second overall
# The data API shares the live account's quota: on the first 429 stop everything instead of
# retrying, and never run past STOP_AT (IST) so the 09:25 OR lock on the server is not starved.
STOP_AT: datetime | None = None
_halt = threading.Event()


def needs(bars: list[tuple], step: int) -> dict[str, tuple[int, set[int]]]:
    """side -> (strike K, offsets K can take vs the minute's ATM after the first break)."""
    orb = [b for b in bars if b[0] <= OR_END]
    post = [b for b in bars if OR_END < b[0] <= EOD]
    if len(orb) < 10 or not post:
        return {}
    hi, lo = max(b[2] for b in orb), min(b[3] for b in orb)
    out = {}
    for side, level, broke in (("CALL", hi, lambda b: b[2] > hi), ("PUT", lo, lambda b: b[3] < lo)):
        first = next((i for i, b in enumerate(post) if broke(b) and b[0] <= ENTRY_CUTOFF), None)
        if first is None:
            continue
        k = round(level / step) * step
        offs = set()
        for b in post[first:]:
            for px in (b[2], b[3], b[4]):
                o = round((k - round(px / step) * step) / step)
                offs.update((o - 1, o, o + 1))
        out[side] = (k, {o for o in offs if -MAX_OFF <= o <= MAX_OFF})
    return out


class Throttled(RuntimeError):
    pass


def call(client: httpx.Client, body: dict) -> dict | None:
    for attempt in range(12):
        if _halt.is_set() or (STOP_AT and datetime.now(IST) >= STOP_AT):
            _halt.set()
            raise Throttled("stopped (deadline or earlier 429)")
        with _lock:
            wait = _last[0] + MIN_GAP - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            _last[0] = time.monotonic()
        try:
            r = client.post(URL, json=body)
        except httpx.HTTPError:
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 429 or "DH-904" in r.text:
            if not _halt.is_set():
                print(f"  HALT: HTTP {r.status_code} {r.text[:150]}", flush=True)
            _halt.set()
            raise Throttled("rate limited (429)")
        if r.status_code in (401, 403) or "DH-901" in r.text:
            raise SystemExit(f"Dhan auth failed: {r.text[:120]} — update DHAN_ACCESS_TOKEN in backend/.env")
        if r.status_code >= 400:
            return None
        d = r.json().get("data") or {}
        return d.get("ce") or d.get("pe") or {}
    raise Throttled("rate limited")


def windows(days: list[date]) -> list[list[date]]:
    out, i = [], 0
    while i < len(days):
        if i + 1 < len(days) and days[i + 1] - days[i] == timedelta(days=1):
            out.append([days[i], days[i + 1]])
            i += 2
        else:
            out.append([days[i]])
            i += 1
    return out


def fetch_window(client, key: str, win: list[date], plan: dict) -> list[tuple]:
    seg, sec, step, _ = CFG[key]
    rows = []
    for side in ("CALL", "PUT"):
        want = {d: plan[d][side] for d in win if side in plan[d]}
        if not want:
            continue
        offs = sorted(set().union(*(o for _, o in want.values())))
        to = win[-1] if len(win) == 2 else win[0] + timedelta(days=1)
        for off in offs:
            body = {"exchangeSegment": seg, "interval": "1", "securityId": sec, "instrument": "OPTIDX",
                    "expiryFlag": "WEEK", "expiryCode": 1, "drvOptionType": side,
                    "strike": "ATM" if off == 0 else f"ATM{off:+d}", "requiredData": FIELDS,
                    "fromDate": win[0].isoformat(), "toDate": to.isoformat()}
            x = call(client, body)
            if not x:
                continue
            ts = x.get("timestamp") or []
            for i in range(len(ts)):
                t = datetime.fromtimestamp(int(ts[i]), IST)
                d, m = t.date(), t.hour * 60 + t.minute
                if d not in want or m > EOD:
                    continue
                try:
                    if float(x["strike"][i]) != want[d][0]:
                        continue
                    rows.append((d.isoformat(), side, want[d][0], m, *(float(x[f][i]) for f in
                                 ("open", "high", "low", "close", "iv", "spot"))))
                except (IndexError, TypeError, ValueError):
                    continue
    return rows


def update(key: str, client: httpx.Client) -> None:
    seg, sec, step, start = CFG[key]
    out, done_p = HIST / f"{key}_opt_1m.csv.gz", HIST / f"{key}_opt_done.json"
    done = set(json.loads(done_p.read_text())) if done_p.exists() else set()
    spot = load(key)
    plan = {d: needs(spot[d], step) for d in sorted(spot) if d >= start and d.isoformat() not in done}
    plan = {d: p for d, p in plan.items() if p}
    wins = windows(sorted(plan))
    print(f"{key}: {len(plan)} days to fetch in {len(wins)} windows, "
          f"~{sum(len(p[s][1]) for p in plan.values() for s in p) // 2 + 1} calls", flush=True)
    new_rows: list[tuple] = []
    t0, failed = time.time(), 0

    def safe(w):
        try:
            return fetch_window(client, key, w, plan)
        except Throttled:
            return None

    with ThreadPoolExecutor(4) as pool:
        for n, (win, rows) in enumerate(zip(wins, pool.map(safe, wins)), 1):
            if rows is None:
                failed += 1          # not marked done, so the next run retries it
            else:
                new_rows += rows
                got = {r[0] for r in rows}      # empty days (contract not yet expired) are retried later
                done.update(d.isoformat() for d in win if d.isoformat() in got)
            if n % 25 == 0 or n == len(wins):
                _save(out, new_rows)
                new_rows = []
                done_p.write_text(json.dumps(sorted(done)))
                print(f"  {key} {n}/{len(wins)} windows ({win[-1]}) {time.time() - t0:.0f}s, "
                      f"rate-limited {failed}", flush=True)
    if failed:
        print(f"{key}: {failed} windows rate-limited — run again later to fill them.", flush=True)


def _save(path: Path, rows: list[tuple]) -> None:
    old = []
    if path.exists():
        with gzip.open(path, "rt", newline="") as f:
            r = csv.reader(f)
            next(r, None)
            old = list(r)
    seen = {(a[0], a[1], a[3]) for a in old}
    merged = old + [[str(v) for v in r] for r in rows if (r[0], r[1], str(r[3])) not in seen]
    merged.sort(key=lambda a: (a[0], a[1], int(a[3])))
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wt", newline="") as f:
        w = csv.writer(f)
        w.writerow(["day", "side", "strike", "minute", "open", "high", "low", "close", "iv", "spot"])
        w.writerows(merged)
    tmp.replace(path)


def main() -> None:
    """Args: index names, plus optional --until HH:MM (IST, today) and --gap SECONDS."""
    global STOP_AT, MIN_GAP
    args = sys.argv[1:]
    if "--until" in args:
        i = args.index("--until")
        hh, mm = map(int, args[i + 1].split(":"))
        STOP_AT = datetime.now(IST).replace(hour=hh, minute=mm, second=0, microsecond=0)
        del args[i:i + 2]
    if "--gap" in args:
        i = args.index("--gap")
        MIN_GAP = float(args[i + 1])
        del args[i:i + 2]
    e = env()
    headers = {"access-token": e["DHAN_ACCESS_TOKEN"], "client-id": e["DHAN_CLIENT_ID"],
               "Content-Type": "application/json", "Accept": "application/json"}
    with httpx.Client(headers=headers, timeout=60) as client:
        for key in args or list(CFG):
            if _halt.is_set():
                print(f"{key}: skipped — stopped (429 or deadline). Run again later.", flush=True)
                continue
            update(key, client)


if __name__ == "__main__":
    main()
