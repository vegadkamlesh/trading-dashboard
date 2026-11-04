"""Dhan access-token expiry alerts on Telegram. Reads only the JWT `exp` claim; no network."""
from __future__ import annotations

import base64
import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set, Tuple

from .config import Settings, get_settings

logger = logging.getLogger("dhan.token")
IST = timezone(timedelta(hours=5, minutes=30))
EVENING_CHECK = (15, 30)     # daily reminder time (server stops 16:00)
SOON_MINUTES = 60


def token_expiry(token: str) -> Optional[datetime]:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return datetime.fromtimestamp(int(json.loads(base64.urlsafe_b64decode(payload))["exp"]), IST)
    except (IndexError, KeyError, ValueError, TypeError, OverflowError, OSError):
        return None


def account_tokens(s: Settings) -> Dict[str, str]:
    tokens = {"master": s.dhan_master_trading_token or s.dhan_access_token}
    if s.dhan_master_trading_token and s.dhan_access_token:
        tokens["master-data"] = s.dhan_access_token
    if s.dhan_child_access_token:
        tokens["child"] = s.dhan_child_access_token
    return {k: v for k, v in tokens.items() if v}


def next_session_start(now: datetime) -> datetime:
    day = now.date() + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return datetime(day.year, day.month, day.day, 9, 0, tzinfo=IST)


def token_alerts(tokens: Dict[str, str], now: datetime, sent: Set[Tuple[str, str, str]],
                 startup: bool) -> List[Tuple[str, str, str]]:
    """Return (level, account, text) alerts not sent before; records them in `sent`."""
    out: List[Tuple[str, str, str]] = []

    def add(level: str, account: str, kind: str, key: str, text: str) -> None:
        if (account, kind, key) not in sent:
            sent.add((account, kind, key))
            out.append((level, account, text))

    for account, token in tokens.items():
        exp = token_expiry(token)
        if exp is None:
            add("warn", account, "unreadable", now.date().isoformat(),
                f"TOKEN {account}: expiry unreadable; verify the Dhan token manually.")
            continue
        key = exp.isoformat()
        left = exp - now
        when = exp.strftime("%d-%b %H:%M")
        if left <= timedelta(0):
            add("error", account, "expired", key,
                f"TOKEN {account} EXPIRED at {when} IST. Algo cannot trade. "
                f"ACTION: new token in .env -> push to AWS.")
        elif left <= timedelta(minutes=SOON_MINUTES):
            add("error", account, "soon", key,
                f"TOKEN {account} expires in {int(left.total_seconds() // 60)} min ({when} IST). "
                f"ACTION: refresh now.")
        elif startup:
            add("info", account, "startup", key, f"TOKEN {account} valid till {when} IST "
                f"({left.total_seconds() / 3600:.1f}h left).")
        if (left > timedelta(0) and (now.hour, now.minute) >= EVENING_CHECK
                and exp < next_session_start(now) + timedelta(minutes=30)):
            add("warn", account, "evening", now.date().isoformat(),
                f"TOKEN {account} expires {when} IST - before next session. "
                f"ACTION: update .env + push to AWS before 9:00 AM.")
    return out


def _emit(alerts: List[Tuple[str, str, str]]) -> None:
    for level, account, text in alerts:
        log = {"error": logger.error, "warn": logger.warning}.get(level, logger.info)
        log(text, extra={"telegram_event": "token_expiry", "telegram_detail": text,
                         "telegram_level": level,
                         "telegram_account": account if account in ("master", "child") else ""})


def token_watch_loop(stop: threading.Event, interval: float = 60.0) -> None:
    sent: Set[Tuple[str, str, str]] = set()
    startup = True
    while not stop.is_set():
        try:
            _emit(token_alerts(account_tokens(get_settings()), datetime.now(IST), sent, startup))
        except Exception:  # noqa: BLE001 - alerting must never stop the app
            logger.exception("Token expiry check failed")
        startup = False
        stop.wait(interval)
