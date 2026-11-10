"""Scheduled high-impact event calendar + the +/- N minute news freeze.

Sources
-------
* ``backend/data/algo2/events_calendar.json`` - the authoritative list. Seeded
  with verified RBI MPC decision days (rbi.org.in, 23-Mar-2026 release) and the
  Union Budget; the operator adds/removes events from the Algo 2 page.
* NSE corporate event calendar (board meetings / results of the heavyweight
  stocks), refreshed once a day. NSE publishes the DATE only, so these entries
  are informational unless ``block_untimed`` is switched on.

There is no reliable free public API for Indian macro events with exact times,
so timed macro events (CPI, GDP, policy) come from the JSON file.
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

import httpx

logger = logging.getLogger("dhan.algo2.news")

NSE_HOME = "https://www.nseindia.com/companies-listing/corporate-filings-event-calendar"
NSE_EVENTS_API = "https://www.nseindia.com/api/event-calendar"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

SEED_EVENTS: List[Dict[str, Any]] = [
    {"date": "2026-12-04", "time": "10:00", "name": "RBI Monetary Policy decision", "impact": "high"},
    {"date": "2027-02-01", "time": "11:00", "name": "Union Budget 2027-28", "impact": "high"},
    {"date": "2027-02-05", "time": "10:00", "name": "RBI Monetary Policy decision", "impact": "high"},
]


def _valid_event(ev: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        day = date.fromisoformat(str(ev.get("date"))).isoformat()
    except ValueError:
        return None
    time_value = ev.get("time")
    if time_value in ("", None):
        time_value = None
    elif not _TIME.match(str(time_value)):
        return None
    name = str(ev.get("name") or "").strip()[:120]
    if not name:
        return None
    return {
        "id": str(ev.get("id") or uuid.uuid4().hex[:10]),
        "date": day,
        "time": time_value,
        "name": name,
        "impact": str(ev.get("impact") or "high"),
        "source": str(ev.get("source") or "manual"),
    }


class NewsEventFetcher:
    def __init__(self, directory: str, window_minutes: int = 15) -> None:
        self.path = os.path.join(directory, "events_calendar.json")
        self.window_minutes = window_minutes
        self.block_untimed = False
        self._events: List[Dict[str, Any]] = []
        self._nse_events: List[Dict[str, Any]] = []
        self._nse_day: Optional[str] = None
        self._nse_error: Optional[str] = None
        self.load()

    # ----------------------------------------------------------- storage
    def load(self) -> None:
        events: Iterable[Dict[str, Any]] = []
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            events = data.get("events", []) if isinstance(data, dict) else []
        except FileNotFoundError:
            events = [dict(ev, source="seed") for ev in SEED_EVENTS]
            self._events = [e for e in (_valid_event(ev) for ev in events) if e]
            self._save()
            return
        except (OSError, ValueError) as exc:
            logger.error("events_calendar.json unreadable (%s); using seed events", exc)
            events = [dict(ev, source="seed") for ev in SEED_EVENTS]
        self._events = [e for e in (_valid_event(ev) for ev in events if isinstance(ev, dict)) if e]

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"events": self._events}, fh, indent=2)
        os.replace(tmp, self.path)

    def add(self, ev: Dict[str, Any]) -> Dict[str, Any]:
        valid = _valid_event({**ev, "id": None, "source": "manual"})
        if valid is None:
            raise ValueError("Event needs date YYYY-MM-DD, optional time HH:MM and a name")
        self._events.append(valid)
        self._events.sort(key=lambda e: (e["date"], e["time"] or ""))
        self._save()
        return valid

    def remove(self, event_id: str) -> bool:
        before = len(self._events)
        self._events = [e for e in self._events if e["id"] != event_id]
        if len(self._events) == before:
            return False
        self._save()
        return True

    # -------------------------------------------------------------- NSE
    async def refresh_nse(self, symbols: Iterable[str], today: date) -> None:
        """Pull heavyweight board-meeting / results dates from NSE once per day (date only)."""
        if self._nse_day == today.isoformat():
            return
        wanted = {s.upper() for s in symbols}
        try:
            async with httpx.AsyncClient(headers=_HEADERS, timeout=15, follow_redirects=True) as client:
                await client.get(NSE_HOME)
                resp = await client.get(NSE_EVENTS_API)
                resp.raise_for_status()
                rows = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            self._nse_error = f"NSE event calendar unavailable: {exc}"
            logger.warning(self._nse_error)
            return
        events: List[Dict[str, Any]] = []
        for row in rows if isinstance(rows, list) else []:
            symbol = str(row.get("symbol") or "").upper()
            if symbol not in wanted:
                continue
            try:
                day = datetime.strptime(str(row.get("date")), "%d-%b-%Y").date()
            except ValueError:
                continue
            if day < today or day > today + timedelta(days=60):
                continue
            purpose = str(row.get("purpose") or row.get("bm_desc") or "Board meeting")[:80]
            events.append({"id": f"nse-{symbol}-{day.isoformat()}", "date": day.isoformat(), "time": None,
                           "name": f"{symbol}: {purpose}", "impact": "medium", "source": "nse"})
        self._nse_events = sorted(events, key=lambda e: e["date"])
        self._nse_day = today.isoformat()
        self._nse_error = None

    # ------------------------------------------------------------ freeze
    def events(self) -> List[Dict[str, Any]]:
        return sorted(self._events + self._nse_events, key=lambda e: (e["date"], e["time"] or ""))

    def is_news_freeze_active(self, now: datetime) -> Tuple[bool, Optional[str]]:
        """True with the event name when ``now`` is within +/- window of a timed event today."""
        today = now.date().isoformat()
        window = timedelta(minutes=self.window_minutes)
        naive_now = now.replace(tzinfo=None)
        for ev in self.events():
            if ev["date"] != today:
                continue
            if ev["time"] is None:
                if self.block_untimed:
                    return True, f"{ev['name']} (all day)"
                continue
            at = datetime.fromisoformat(f"{ev['date']}T{ev['time']}")
            if at - window <= naive_now <= at + window:
                return True, ev["name"]
        return False, None

    def status(self, now: datetime) -> Dict[str, Any]:
        active, name = self.is_news_freeze_active(now)
        today = now.date().isoformat()
        return {
            "freezeActive": active,
            "freezeEvent": name,
            "windowMinutes": self.window_minutes,
            "blockUntimed": self.block_untimed,
            "today": [e for e in self.events() if e["date"] == today],
            "upcoming": [e for e in self.events() if e["date"] >= today][:40],
            "nseError": self._nse_error,
            "nseRefreshedFor": self._nse_day,
        }
