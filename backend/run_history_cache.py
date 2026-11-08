"""Local 1-minute index history cache (NIFTY / SENSEX) from Dhan /charts/intraday.

    .venv\\Scripts\\python.exe run_history_cache.py           # back-fill once, then append missing days
    .venv\\Scripts\\python.exe run_history_cache.py NIFTY     # one index only

Files: backend/data/history/<INDEX>_1m.csv.gz (ts_ist,open,high,low,close); backend/data/ is git-ignored.
Read-only market-data calls; never places orders.
"""
from __future__ import annotations

import csv
import gzip
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data" / "history"
IST = timezone(timedelta(hours=5, minutes=30))
INDICES = {"NIFTY": "13", "SENSEX": "51"}
EARLIEST = date(2015, 1, 1)
CHUNK_DAYS = 89
URL = "https://api.dhan.co/v2/charts/intraday"


def env() -> dict:
    out = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def path_for(key: str) -> Path:
    return OUT / f"{key}_1m.csv.gz"


def load(key: str) -> list[tuple]:
    p = path_for(key)
    if not p.exists():
        return []
    with gzip.open(p, "rt", newline="") as f:
        r = csv.reader(f)
        next(r, None)
        return [(row[0], *map(float, row[1:5])) for row in r]


def save(key: str, rows: list[tuple]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = path_for(key).with_suffix(".tmp")
    with gzip.open(tmp, "wt", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts_ist", "open", "high", "low", "close"])
        w.writerows(rows)
    tmp.replace(path_for(key))


def fetch(client: httpx.Client, sec: str, a: date, b: date) -> list[tuple]:
    # Dhan's fromDate is exclusive, so start at 09:14 to keep the 09:15 candle.
    body = {"securityId": sec, "exchangeSegment": "IDX_I", "instrument": "INDEX", "interval": "1",
            "oi": False, "fromDate": f"{a} 09:14:00", "toDate": f"{b} 15:31:00"}
    for attempt in range(6):
        r = client.post(URL, json=body)
        if r.status_code == 429 or "DH-904" in r.text:
            time.sleep(1.5 * (attempt + 1))
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"{r.status_code} {r.text[:160]}")
        d = r.json()
        ts, o, h, l, c = (d.get(k) or [] for k in ("timestamp", "open", "high", "low", "close"))
        rows = []
        for i in range(min(len(ts), len(o), len(h), len(l), len(c))):
            t = datetime.fromtimestamp(int(ts[i]), IST)
            if 9 * 60 + 15 <= t.hour * 60 + t.minute <= 15 * 60 + 29:
                rows.append((t.strftime("%Y-%m-%d %H:%M"), float(o[i]), float(h[i]), float(l[i]), float(c[i])))
        return rows
    raise RuntimeError("rate limited")


def update(key: str, sec: str, client: httpx.Client) -> None:
    rows = load(key)
    have = {r[0] for r in rows}
    start = (date.fromisoformat(rows[-1][0][:10]) + timedelta(days=1)) if rows else EARLIEST
    today = datetime.now(IST).date()
    added, a = 0, start
    while a <= today:
        b = min(a + timedelta(days=CHUNK_DAYS), today)
        try:
            got = fetch(client, sec, a, b)
        except RuntimeError as exc:
            print(f"  {key} {a}..{b}: {exc}", flush=True)
            got = []
        new = [r for r in got if r[0] not in have]
        rows.extend(new)
        have.update(r[0] for r in new)
        added += len(new)
        print(f"  {key} {a}..{b}: +{len(new)}", flush=True)
        a = b + timedelta(days=1)
        time.sleep(0.35)
    rows.sort(key=lambda r: r[0])
    save(key, rows)
    days = len({r[0][:10] for r in rows})
    print(f"{key}: +{added} candles, total {len(rows)} candles / {days} days "
          f"({rows[0][0][:10] if rows else '-'} .. {rows[-1][0][:10] if rows else '-'})", flush=True)


def main() -> None:
    e = env()
    headers = {"access-token": e["DHAN_ACCESS_TOKEN"], "client-id": e["DHAN_CLIENT_ID"],
               "Content-Type": "application/json", "Accept": "application/json"}
    with httpx.Client(headers=headers, timeout=30) as client:
        for key in sys.argv[1:] or list(INDICES):
            update(key, INDICES[key], client)


if __name__ == "__main__":
    main()
