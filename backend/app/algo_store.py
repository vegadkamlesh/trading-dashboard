"""Disk persistence for the ORB algo: restart-safe state + a rolling journal.

Layout (backend/data/algo/):
    state.json                 current live state (survives a restart)
    journal-YYYY-MM-DD.jsonl   append-only event log for that day
    trades.jsonl               every closed trade, ever (for the equity curve)

Retention
---------
Journals older than JOURNAL_KEEP_DAYS (default 7) are deleted automatically:
once at startup and once a day thereafter. trades.jsonl is tiny and kept
forever so the equity curve / compounding history is never lost.

Everything here is synchronous, tiny file I/O on the event loop. That is
deliberate: a JSONL append is a few microseconds and we would rather have a
guaranteed-ordered log than an async writer that can lose the last event when
the process dies.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger("dhan.algo.store")

JOURNAL_KEEP_DAYS = 7
_BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "algo")


def base_dir() -> str:
    return _BASE


class AlgoStore:
    def __init__(self, directory: str | None = None) -> None:
        self.dir = directory or _BASE
        self._ensure()

    def _ensure(self) -> None:
        try:
            os.makedirs(self.dir, exist_ok=True)
        except OSError as exc:  # pragma: no cover
            logger.error("Cannot create algo data dir %s: %s", self.dir, exc)

    def _path(self, name: str) -> str:
        return os.path.join(self.dir, name)

    # ---------------- state ----------------
    def save_state(self, state: Dict[str, Any]) -> None:
        tmp = self._path("state.json.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(state, fh, indent=2, default=str)
            os.replace(tmp, self._path("state.json"))
        except OSError as exc:
            logger.warning("Failed to persist algo state: %s", exc)

    def load_state(self) -> Optional[Dict[str, Any]]:
        path = self._path("state.json")
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else None
        except (OSError, ValueError) as exc:
            logger.warning("Failed to read algo state: %s", exc)
            return None

    # ---------------- journal ----------------
    @staticmethod
    def _journal_name(day: str) -> str:
        return f"journal-{day}.jsonl"

    def append_event(self, event: Dict[str, Any], day: Optional[str] = None) -> None:
        day = day or date.today().isoformat()
        event = {**event, "ts": event.get("ts") or time.time(),
                 "time": event.get("time") or datetime.now().strftime("%H:%M:%S")}
        try:
            with open(self._path(self._journal_name(day)), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, default=str) + "\n")
        except OSError as exc:
            logger.warning("Failed to append algo journal: %s", exc)

    def read_journal(self, days: int = JOURNAL_KEEP_DAYS) -> List[Dict[str, Any]]:
        """Newest day first, oldest event first inside each day."""
        out: List[Dict[str, Any]] = []
        for day in self._journal_days(limit=days):
            out.extend(self.read_day(day))
        return out

    def read_day(self, day: str) -> List[Dict[str, Any]]:
        path = self._path(self._journal_name(day))
        if not os.path.exists(path):
            return []
        rows: List[Dict[str, Any]] = []
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            return []
        return rows

    def _journal_days(self, limit: int = JOURNAL_KEEP_DAYS) -> List[str]:
        days: List[str] = []
        try:
            for name in os.listdir(self.dir):
                if name.startswith("journal-") and name.endswith(".jsonl"):
                    days.append(name[len("journal-"):-len(".jsonl")])
        except OSError:
            return []
        days.sort(reverse=True)
        return days[:limit]

    def journal_days(self) -> List[str]:
        return self._journal_days(limit=100)

    def prune_journals(self, keep_days: int = JOURNAL_KEEP_DAYS) -> int:
        """Delete journal files older than `keep_days`. Returns files removed."""
        cutoff = (date.today() - timedelta(days=keep_days)).isoformat()
        removed = 0
        try:
            for name in os.listdir(self.dir):
                if not (name.startswith("journal-") and name.endswith(".jsonl")):
                    continue
                day = name[len("journal-"):-len(".jsonl")]
                if day < cutoff:
                    try:
                        os.remove(self._path(name))
                        removed += 1
                    except OSError:
                        pass
        except OSError:
            return 0
        if removed:
            logger.info("Pruned %d algo journal file(s) older than %s", removed, cutoff)
        return removed

    # ---------------- trades ----------------
    def append_trade(self, trade: Dict[str, Any]) -> None:
        try:
            with open(self._path("trades.jsonl"), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(trade, default=str) + "\n")
        except OSError as exc:
            logger.warning("Failed to append trade: %s", exc)

    def read_trades(self, limit: int = 500) -> List[Dict[str, Any]]:
        path = self._path("trades.jsonl")
        if not os.path.exists(path):
            return []
        rows: List[Dict[str, Any]] = []
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            return []
        return rows[-limit:]

    def recent_trades(self, days: int = 7) -> List[Dict[str, Any]]:
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        return [t for t in self.read_trades(limit=5000) if str(t.get("date", "")) >= cutoff]


store = AlgoStore()
