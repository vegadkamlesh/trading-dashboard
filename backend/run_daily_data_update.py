"""Daily market-data update (run after market close; Windows Task Scheduler runs it Mon-Fri 16:30).

    .venv\\Scripts\\python.exe run_daily_data_update.py

Steps (each isolated, so one failure does not stop the rest), Master Dhan credentials only:
  1. run_history_cache.py        NIFTY + SENSEX 1-minute index candles
  2. run_expiry_calendar.py      exchange expiry calendar (NSE/BSE bhavcopy)
  3. EOD option-chain snapshot   both indices, nearest 2 expiries, all strikes (LTP/OI/IV/greeks)
  4. run_option_cache.py         premium of the traded strike (Dhan publishes it after the contract expires,
                                 so recent days are retried automatically on later runs)
  5. run_orb_option_backtest.py  refresh the report
A short summary goes to Telegram when TELEGRAM_* is set in backend/.env. Never places orders.
"""
from __future__ import annotations

import gzip
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from run_history_cache import env

ROOT = Path(__file__).resolve().parent
HIST = ROOT / "data" / "history"
CHAIN_DIR = HIST / "optionchain"
LOG = HIST / "daily_update.log"
IST = timezone(timedelta(hours=5, minutes=30))
CHAIN = {"NIFTY": 13, "SENSEX": 51}
PY = sys.executable


def log(msg: str) -> None:
    line = f"{datetime.now(IST):%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def step(name: str, args: list[str], timeout: int) -> bool:
    t = time.time()
    try:
        p = subprocess.run([PY, "-u", *args], cwd=ROOT, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        tail = " | ".join(x.strip() for x in (p.stdout + p.stderr).strip().splitlines()[-3:])
        ok = p.returncode == 0
    except subprocess.TimeoutExpired:
        ok, tail = False, f"timeout {timeout}s"
    log(f"{'OK ' if ok else 'ERR'} {name} ({time.time() - t:.0f}s): {tail[:400]}")
    return ok


def chain_snapshot(e: dict) -> bool:
    headers = {"access-token": e["DHAN_ACCESS_TOKEN"], "client-id": e["DHAN_CLIENT_ID"],
               "Content-Type": "application/json", "Accept": "application/json"}
    now = datetime.now(IST)
    ok = True
    with httpx.Client(headers=headers, timeout=30) as c:
        for key, sec in CHAIN.items():
            try:
                r = c.post("https://api.dhan.co/v2/optionchain/expirylist",
                           json={"UnderlyingScrip": sec, "UnderlyingSeg": "IDX_I"})
                r.raise_for_status()
                expiries = (r.json().get("data") or [])[:2]
                for exp in expiries:
                    time.sleep(3.5)          # Dhan: 1 option-chain request per 3 s
                    r = c.post("https://api.dhan.co/v2/optionchain",
                               json={"UnderlyingScrip": sec, "UnderlyingSeg": "IDX_I", "Expiry": exp})
                    r.raise_for_status()
                    out = CHAIN_DIR / key / f"{now:%Y-%m-%d}_{exp}.json.gz"
                    out.parent.mkdir(parents=True, exist_ok=True)
                    with gzip.open(out, "wt", encoding="utf-8") as f:
                        json.dump({"taken_at": now.isoformat(), "index": key, "expiry": exp,
                                   "data": r.json().get("data")}, f)
                log(f"OK  option chain {key} {expiries}")
            except Exception as exc:          # noqa: BLE001 - logged, next index still runs
                ok = False
                log(f"ERR option chain {key}: {str(exc)[:200]}")
            time.sleep(3.5)
    return ok


def telegram(e: dict, text: str) -> None:
    if str(e.get("TELEGRAM_ENABLED", "")).lower() not in ("1", "true", "yes") or not e.get("TELEGRAM_BOT_TOKEN"):
        return
    try:
        httpx.post(f"https://api.telegram.org/bot{e['TELEGRAM_BOT_TOKEN']}/sendMessage",
                   json={"chat_id": e["TELEGRAM_CHAT_ID"], "text": text}, timeout=20)
    except httpx.HTTPError:
        pass


def main() -> None:
    HIST.mkdir(parents=True, exist_ok=True)
    e = env()
    log("=== daily data update start")
    res = {
        "index candles": step("history cache", ["run_history_cache.py"], 1800),
        "expiry calendar": step("expiry calendar", ["run_expiry_calendar.py"], 1800),
        "option chain": chain_snapshot(e),
    }
    # The option download stops itself on the first 429 and never runs into the next session's morning.
    until = "23:59" if datetime.now(IST).hour >= 15 else "08:30"
    res["option premiums"] = step("option cache", ["run_option_cache.py", "--until", until, "--gap", "1.0"], 4 * 3600)
    res["backtest report"] = step("backtest", ["run_orb_option_backtest.py"], 3600)
    summary = "Data update " + datetime.now(IST).strftime("%d-%b %H:%M") + "\n" + "\n".join(
        f"{'✅' if v else '❌'} {k}" for k, v in res.items())
    log(summary.replace("\n", " | "))
    telegram(e, summary + ("" if all(res.values()) else "\nDetails: backend/data/history/daily_update.log"))


if __name__ == "__main__":
    main()
