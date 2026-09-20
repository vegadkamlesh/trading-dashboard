"""Central logging setup + automatic log cleanup.

Everything the app does is written to rotating daily log files under
`backend/logs/`. This makes debugging easy ("kya hua" check karne ke liye).

Files created in `backend/logs/`:
  * app-YYYY-MM-DD.log      -> all app logs (INFO and above)
  * errors-YYYY-MM-DD.log   -> only WARNING/ERROR (failures, so you scan fast)
  * orders-YYYY-MM-DD.log   -> every order attempt (preview/place/cancel)
  * access-YYYY-MM-DD.log   -> API requests (method, path, status, time)

Retention: files older than `LOG_RETENTION_DAYS` (default 7) are deleted at
startup and once per day. So disk space is used for last-1-week only.
"""
from __future__ import annotations

import logging
import logging.handlers
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import get_settings

_initialised = False


def _log_dir() -> Path:
    s = get_settings()
    p = Path(s.log_dir)
    if not p.is_absolute():
        # relative to backend/ (the parent of the app package)
        p = Path(__file__).resolve().parent.parent / p
    p.mkdir(parents=True, exist_ok=True)
    return p


class DailyFileHandler(logging.Handler):
    """Writes to `<prefix>-YYYY-MM-DD.log`, rolling to a new file each day."""

    def __init__(self, directory: Path, prefix: str, level: int = logging.NOTSET):
        super().__init__(level)
        self._dir = directory
        self._prefix = prefix
        self._current_day: date | None = None
        self._stream = None

    def _path_for(self, day: date) -> Path:
        return self._dir / f"{self._prefix}-{day.isoformat()}.log"

    def _ensure_stream(self) -> None:
        today = date.today()
        if self._stream is None or self._current_day != today:
            if self._stream is not None:
                self._stream.close()
            self._stream = open(self._path_for(today), "a", encoding="utf-8")
            self._current_day = today

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._ensure_stream()
            assert self._stream is not None
            self._stream.write(self.format(record) + "\n")
            self._stream.flush()
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        try:
            if self._stream is not None:
                self._stream.close()
                self._stream = None
        finally:
            super().close()


def cleanup_old_logs(retention_days: int | None = None) -> int:
    """Delete log files older than the retention window. Returns count deleted."""
    s = get_settings()
    days = retention_days if retention_days is not None else s.log_retention_days
    cutoff = date.today() - timedelta(days=max(1, days))
    removed = 0
    try:
        directory = _log_dir()
    except Exception:
        return 0
    import re

    date_re = re.compile(r"(\d{4}-\d{2}-\d{2})")
    for f in directory.glob("*.log"):
        m = date_re.search(f.stem)  # e.g. "app-2026-01-01"
        if not m:
            continue
        try:
            d = datetime.strptime(m.group(1), "%Y-%m-%d").date()
        except ValueError:
            continue
        if d < cutoff:
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def setup_logging() -> Path:
    """Configure root logging. Safe to call multiple times (idempotent)."""
    global _initialised
    s = get_settings()
    level = getattr(logging, s.log_level.upper(), logging.INFO)
    directory = _log_dir()

    if _initialised:
        return directory

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(level)

    # Console (so you see live output too)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.setLevel(level)
    root.addHandler(console)

    # app-*.log  (everything at/above configured level)
    app_handler = DailyFileHandler(directory, "app", level)
    app_handler.setFormatter(fmt)
    root.addHandler(app_handler)

    # errors-*.log (WARNING and above, regardless of console verbosity)
    err_handler = DailyFileHandler(directory, "errors", logging.WARNING)
    err_handler.setFormatter(fmt)
    root.addHandler(err_handler)

    # Dedicated order + access loggers (propagate=False so we control files).
    _attach_dedicated("dhan.orders", "orders", directory, fmt)
    _attach_dedicated("dhan.access", "access", directory, fmt)

    _initialised = True
    logger = logging.getLogger("dhan.logsetup")
    logger.info("Logging to %s (retention=%d days, level=%s)", directory, s.log_retention_days, s.log_level.upper())
    removed = cleanup_old_logs()
    if removed:
        logger.info("Cleaned up %d old log file(s)", removed)
    return directory


def _attach_dedicated(name: str, prefix: str, directory: Path, fmt: logging.Formatter) -> None:
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = True  # still go to root (console + app.log)
    if any(isinstance(h, DailyFileHandler) for h in logger.handlers):
        return
    h = DailyFileHandler(directory, prefix, logging.INFO)
    h.setFormatter(fmt)
    logger.addHandler(h)


def daily_cleanup_loop() -> None:
    """Blocking loop that runs cleanup once a day. Run in a background thread."""
    while True:
        # Sleep ~6h; cheap, and guarantees we clean within a day of rollover.
        time.sleep(6 * 3600)
        try:
            n = cleanup_old_logs()
            if n:
                logging.getLogger("dhan.logsetup").info("Daily cleanup removed %d log file(s)", n)
        except Exception:
            pass
