"""Daily, append-only archive of everything the app records to a private S3 bucket.

Uploaded once per day after market close (15:37 IST, retried every 15 min until it succeeds):
  * ``backend/data``  -> ``s3://<bucket>/data/``  (Algo 1/2 journals, trades, research history,
    microstructure, daily cache)
  * log directory     -> ``s3://<bucket>/logs/``
  * audit databases   -> ``s3://<bucket>/db/<day>/`` (consistent SQLite snapshots)

``aws s3 sync`` never deletes remote objects and the server role has no delete permission, so
the bucket only grows. Local copies are removed only after the archive has them: log files dated
before the last successful archive, and raw microstructure days older than
``ARCHIVE_LOCAL_MICRO_DAYS`` once a dry-run sync shows nothing left to upload.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sqlite3
import time as _time
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from .config import get_settings

logger = logging.getLogger("dhan.archive")

IST = ZoneInfo("Asia/Kolkata")
BACKEND = Path(__file__).resolve().parent.parent
MARKET_OPEN, ARCHIVE_AFTER = time(9, 0), time(15, 37)
RETRY_SECONDS = 15 * 60
SYNC_TIMEOUT_SECONDS = 1800
STATE_FILE = "archive_state.json"


def _data_dir() -> Path:
    return BACKEND / "data"


def _log_dir() -> Path:
    p = Path(get_settings().log_dir)
    return p if p.is_absolute() else BACKEND / p


def _state_path() -> Path:
    return _data_dir() / STATE_FILE


def last_success_day() -> Optional[date]:
    try:
        return date.fromisoformat(json.loads(_state_path().read_text(encoding="utf-8"))["lastSuccessDay"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def enabled() -> bool:
    return bool(get_settings().archive_bucket.strip())


def due(now: datetime, last_ok: Optional[date], last_attempt: float) -> bool:
    """Run after the close (or any time outside market hours) once per day; never while trading."""
    if last_ok == now.date() or _time.monotonic() - last_attempt < RETRY_SECONDS:
        return False
    return now.weekday() >= 5 or not (MARKET_OPEN <= now.time() < ARCHIVE_AFTER)


async def _aws(*args: str) -> tuple[int, str]:
    exe = shutil.which("aws") or "/usr/bin/aws"
    proc = await asyncio.create_subprocess_exec(
        exe, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), SYNC_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "timed out"
    return proc.returncode or 0, out.decode("utf-8", "replace").strip()


async def _sync(src: Path, dest: str, *, dryrun: bool = False) -> tuple[int, str]:
    args = ["s3", "sync", str(src), dest, "--only-show-errors", "--no-progress"]
    if dryrun:
        args = ["s3", "sync", str(src), dest, "--dryrun", "--no-progress"]
    return await _aws(*args)


def _snapshot_dbs(work: Path) -> list[Path]:
    work.mkdir(parents=True, exist_ok=True)
    out = []
    for db in sorted(BACKEND.glob("audit*.sqlite3")):
        target = work / db.name
        src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            dst = sqlite3.connect(target)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        out.append(target)
    return out


async def _prune_microstructure(bucket: str, today: date) -> list[str]:
    root = _data_dir() / "algo2" / "microstructure"
    keep_from = today - timedelta(days=get_settings().archive_local_micro_days)
    removed = []
    for day_dir in sorted(root.glob("????-??-??")) if root.is_dir() else []:
        try:
            if date.fromisoformat(day_dir.name) >= keep_from:
                continue
        except ValueError:
            continue
        rel = day_dir.relative_to(_data_dir()).as_posix()
        code, pending = await _sync(day_dir, f"s3://{bucket}/data/{rel}/", dryrun=True)
        if code == 0 and not pending:
            shutil.rmtree(day_dir, ignore_errors=True)
            removed.append(day_dir.name)
        else:
            logger.warning("Microstructure %s kept locally: archive not verified (%s)", day_dir.name, pending[:200])
    return removed


async def archive_once(today: date) -> bool:
    bucket = get_settings().archive_bucket.strip()
    if not bucket:
        return False
    started = _time.monotonic()
    failures = []
    for src, prefix in ((_data_dir(), "data"), (_log_dir(), "logs")):
        if src.is_dir():
            code, out = await _sync(src, f"s3://{bucket}/{prefix}/")
            if code != 0:
                failures.append(f"{prefix}: {out[:300]}")
    # /opt is read-only under systemd ProtectSystem; the data dir's parent is the writable volume.
    work = _data_dir().resolve().parent / "archive-tmp"
    try:
        for snap in await asyncio.to_thread(_snapshot_dbs, work):
            code, out = await _aws("s3", "cp", str(snap), f"s3://{bucket}/db/{today.isoformat()}/{snap.name}",
                                   "--only-show-errors", "--no-progress")
            if code != 0:
                failures.append(f"db {snap.name}: {out[:300]}")
    except (OSError, sqlite3.Error) as exc:
        failures.append(f"db snapshot: {exc}")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if failures:
        logger.warning("Daily S3 archive FAILED (nothing deleted locally; retry in 15 min): %s", " | ".join(failures))
        return False
    _state_path().write_text(json.dumps({"lastSuccessDay": today.isoformat(), "at": _time.time()}),
                             encoding="utf-8")
    removed = await _prune_microstructure(bucket, today)
    free_gb = shutil.disk_usage(_data_dir()).free / 2 ** 30
    text = (f"🗄️ Daily archive saved to S3 ({_time.monotonic() - started:.0f}s)\n"
            f"Logs, journals, research, microstructure and audit DB are kept permanently\n"
            f"Server disk free: {free_gb:.1f} GB"
            + (f"\nArchived microstructure removed from server: {', '.join(removed)}" if removed else ""))
    logger.info(text.replace("\n", " | "), extra={"telegram_event": "archive", "telegram_detail": text})
    return True


async def archive_loop() -> None:
    if not enabled():
        return
    last_attempt = -RETRY_SECONDS
    while True:
        try:
            now = datetime.now(IST)
            if due(now, last_success_day(), last_attempt):
                last_attempt = _time.monotonic()
                await archive_once(now.date())
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - archiving must never affect trading
            logger.exception("Daily S3 archive crashed; will retry")
        await asyncio.sleep(60)
