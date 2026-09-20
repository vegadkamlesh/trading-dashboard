"""Local SQLite audit log of every order attempt (dry-run or live).

Used to produce an end-of-day summary. Path is backend/audit.sqlite3, which is
git-ignored. No secrets are stored here — only order params + result.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import get_settings

logger = logging.getLogger("dhan.audit")
order_logger = logging.getLogger("dhan.orders")

DB_PATH = Path(__file__).resolve().parent.parent / "audit.sqlite3"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS order_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                action TEXT NOT NULL,
                index_key TEXT,
                option_type TEXT,
                transaction_type TEXT,
                quantity INTEGER,
                price REAL,
                target_price REAL,
                stop_loss_price REAL,
                dry_run INTEGER NOT NULL,
                success INTEGER NOT NULL,
                order_id TEXT,
                status TEXT,
                message TEXT,
                payload TEXT
            )
            """
        )
        conn.commit()


def log_order(
    *,
    action: str,
    success: bool,
    dry_run: bool,
    order_id: Optional[str] = None,
    status: Optional[str] = None,
    message: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
    index_key: Optional[str] = None,
    option_type: Optional[str] = None,
    transaction_type: Optional[str] = None,
    quantity: Optional[int] = None,
    price: Optional[float] = None,
    target_price: Optional[float] = None,
    stop_loss_price: Optional[float] = None,
) -> None:
    try:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO order_audit (
                    ts, action, index_key, option_type, transaction_type, quantity,
                    price, target_price, stop_loss_price, dry_run, success,
                    order_id, status, message, payload
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    time.time(), action, index_key, option_type, transaction_type,
                    quantity, price, target_price, stop_loss_price,
                    1 if dry_run else 0, 1 if success else 0,
                    order_id, status, message,
                    json.dumps(payload or {}, default=str),
                ),
            )
            conn.commit()
    except Exception as exc:  # never let audit failure break trading
        logger.error("audit log failed: %s", exc)
    else:
        # Also write a human-readable line to the dedicated orders-*.log file.
        try:
            order_logger.info(
                "%s %s %s %s qty=%s price=%s target=%s sl=%s dryRun=%s success=%s "
                "orderId=%s status=%s msg=%s",
                action,
                index_key or "-",
                option_type or "-",
                transaction_type or "-",
                quantity, price, target_price, stop_loss_price,
                dry_run, success, order_id or "-", status or "-", message or "-",
            )
        except Exception:
            pass


def prune_old_logs(retention_days: int | None = None) -> int:
    """Delete audit rows older than `retention_days` (default from settings)."""
    s = get_settings()
    days = retention_days if retention_days is not None else s.log_retention_days
    cutoff = time.time() - max(1, days) * 86400
    try:
        with _connect() as conn:
            cur = conn.execute("DELETE FROM order_audit WHERE ts < ?", (cutoff,))
            conn.commit()
            return cur.rowcount or 0
    except Exception as exc:
        logger.error("audit prune failed: %s", exc)
        return 0


def eod_summary(day_start_ts: Optional[float] = None) -> Dict[str, Any]:
    if day_start_ts is None:
        # Local midnight-ish; caller can override.
        day_start_ts = time.time() - 24 * 3600
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM order_audit WHERE ts >= ? ORDER BY ts ASC",
            (day_start_ts,),
        ).fetchall()
    entries: List[Dict[str, Any]] = [dict(r) for r in rows]
    live = [e for e in entries if not e["dry_run"]]
    success = [e for e in live if e["success"]]
    return {
        "generatedAt": time.time(),
        "totalAttempts": len(entries),
        "dryRunAttempts": len(entries) - len(live),
        "liveAttempts": len(live),
        "liveSuccess": len(success),
        "liveFailed": len(live) - len(success),
        "entries": entries,
    }
