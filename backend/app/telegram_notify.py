"""Opt-in, metadata-only alerts; network work never runs on the trading loop."""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import Settings, get_settings

logger = logging.getLogger("dhan.telegram")

EVENTS = {
    "startup": "Backend starting; verify readiness in dashboard",
    "shutdown": "Backend shutting down",
    "test": "Telegram test notification",
    "heartbeat": "Notification worker heartbeat (not broker/trading readiness)",
    "algo_update": "Algo activity recorded; check dashboard journal",
    "algo_armed": "Algo armed by operator",
    "auto_armed": "Startup defaults applied: algo auto-armed for the default accounts",
    "auto_arm_failed": "Startup auto-arm FAILED or partial; algo may NOT trade. Check dashboard now",
    "algo_disarmed": "Algo disarmed; new entries stopped, open-position management continues",
    "algo_entry": "Algo entry confirmed",
    "algo_exit": "Algo exit recorded; check journal for reconciliation/estimates",
    "order_success": "Order action recorded; not a fill confirmation",
    "order_failed": "Order action failed/unresolved; check broker before retrying",
    "mode_changed": "Trading safety mode changed by operator",
    "fast_changed": "Order PIN safety setting changed by operator",
    "broker_update": "Broker order update received; holdings require reconciliation",
    "feed_connected": "Market feed connected (not trading readiness)",
    "login_failed": "Dashboard broker-token validation failed",
    "api_error": "Dashboard API returned a server error",
    "warning": "Application warning; inspect local logs",
    "error": "Application error; inspect local logs",
}


class NotificationHandler(logging.Handler):
    def __init__(self, service: TelegramNotifier) -> None:
        super().__init__(logging.INFO)
        self.service = service

    def emit(self, record: logging.LogRecord) -> None:
        if not record.name.startswith("dhan.") or record.name.startswith("dhan.telegram"):
            return
        event = getattr(record, "telegram_event", None)
        if event not in EVENTS:
            if record.levelno < logging.WARNING:
                return
            event = "error" if record.levelno >= logging.ERROR else "warning"
        elif event == "algo_update" and record.levelno >= logging.WARNING:
            event = "error" if record.levelno >= logging.ERROR else "warning"
        # Never export record messages, arguments, exception text, URLs or payloads.
        account = getattr(record, "telegram_account", "")
        mode = getattr(record, "telegram_mode", "")
        self.service.publish(event, account=account, mode=mode)


class TelegramNotifier:
    SEND_INTERVAL_SECONDS = 3.1  # Also respects Telegram's 20 messages/minute group limit.
    QUEUE_SIZE = 100
    MAX_EVENT_AGE_SECONDS = 600

    def __init__(self) -> None:
        self._queue: queue.Queue[tuple[float, str]] = queue.Queue(self.QUEUE_SIZE)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._settings: Settings | None = None
        self._handler = NotificationHandler(self)
        self._recent: dict[tuple[str, str], float] = {}
        self._sent = self._failed = self._dropped = self._suppressed = 0
        self._last_sent_at: float | None = None
        self._last_error: str | None = None

    def start(self, settings: Settings | None = None) -> None:
        if self._thread is not None and self._thread.is_alive():
            if self._stop.is_set():
                raise RuntimeError("Previous Telegram worker is still stopping")
            return
        self._settings = settings or get_settings()
        if not self._settings.telegram_enabled:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="telegram-notifications", daemon=True)
        self._thread.start()
        logging.getLogger().addHandler(self._handler)
        self.publish("startup")

    def status(self) -> dict[str, object]:
        with self._lock:
            return {
                "enabled": bool(self._settings and self._settings.telegram_enabled),
                "running": bool(self._thread and self._thread.is_alive() and not self._stop.is_set()),
                "queued": self._queue.qsize(),
                "sent": self._sent,
                "failed": self._failed,
                "dropped": self._dropped,
                "suppressed": self._suppressed,
                "lastSentAt": self._last_sent_at,
                "lastError": self._last_error,
            }

    def publish(self, event: str, *, account: str = "", mode: str = "") -> bool:
        if event not in EVENTS:
            raise ValueError("Unknown Telegram notification event")
        if (not self._settings or not self._settings.telegram_enabled or self._stop.is_set()
                or self._thread is None or not self._thread.is_alive()):
            return False
        now = time.monotonic()
        account = account if account in ("master", "child") else ""
        mode = mode if mode in ("DRY-RUN", "LIVE", "PIN REQUIRED", "PIN SKIPPED") else ""
        with self._lock:
            if event in ("warning", "error", "api_error", "login_failed"):
                key = (event, account)
                previous = self._recent.get(key)
                if previous is not None and now - previous < 60:
                    self._suppressed += 1
                    return False
                self._recent[key] = now
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        text = f"Trading dashboard | {timestamp}\n{EVENTS[event]}"
        if event == "heartbeat":
            snapshot = self.status()
            text += (
                f"\nSent={snapshot['sent']} failed={snapshot['failed']}"
                f" dropped={snapshot['dropped']} grouped={snapshot['suppressed']}"
            )
        if account:
            text += f"\nAccount: {account}"
        if mode:
            text += f"\nMode: {mode}"
        try:
            self._queue.put_nowait((now, text))
        except queue.Full:
            with self._lock:
                self._dropped += 1
                self._last_error = "Notification queue full; event dropped"
            logger.error("Telegram notification queue full; event dropped (trading unaffected)")
            return False
        return True

    def stop(self) -> None:
        logging.getLogger().removeHandler(self._handler)
        thread = self._thread
        if thread is None or not thread.is_alive():
            return
        self.publish("shutdown")
        self._stop.set()
        thread.join(timeout=10)
        if thread.is_alive():
            self._record_failure("Notification worker shutdown timed out")

    def _record_failure(self, reason: str) -> None:
        with self._lock:
            self._failed += 1
            self._last_error = reason
        logger.error("Telegram delivery failed: %s (trading unaffected)", reason)

    def _send(self, text: str) -> bool:
        settings = self._settings
        if settings is None:
            raise RuntimeError("Telegram worker started without settings")
        request = Request(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
            data=json.dumps({"chat_id": settings.telegram_chat_id, "text": text}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        for attempt in range(3):
            delay = max(self.SEND_INTERVAL_SECONDS, 2 ** (attempt + 1))
            try:
                # urllib does not emit HTTP request URLs (which contain the bot token).
                with urlopen(request, timeout=5) as response:
                    body = json.loads(response.read(65536))
                if not isinstance(body, dict) or body.get("ok") is not True:
                    self._record_failure("Telegram API did not confirm delivery")
                    return False
                with self._lock:
                    self._sent += 1
                    self._last_sent_at = time.time()
                    self._last_error = None
                return True
            except HTTPError as exc:
                code = exc.code
                try:
                    body = json.loads(exc.read(65536))
                except (ValueError, OSError):
                    body = {}
                finally:
                    exc.close()
                if code == 429 and isinstance(body, dict):
                    parameters = body.get("parameters")
                    retry_after = parameters.get("retry_after") if isinstance(parameters, dict) else None
                    if isinstance(retry_after, (int, float)) and 0 < retry_after <= 60:
                        delay = max(self.SEND_INTERVAL_SECONDS, retry_after)
                    elif retry_after is not None:
                        self._record_failure("Telegram rate limit exceeds retry window")
                        return False
                elif code < 500:
                    self._record_failure(f"Telegram HTTP {code}; check local bot/channel configuration")
                    return False
                reason = f"Telegram HTTP {code}; retry limit reached"
            except (URLError, OSError) as exc:
                # Exception strings can contain the credential-bearing request URL.
                reason = f"Telegram network failure ({type(exc).__name__}); delivery may be uncertain"
            except ValueError:
                self._record_failure("Invalid Telegram API response")
                return False
            if attempt == 2 or self._stop.wait(delay):
                self._record_failure(reason)
                return False
        return False

    def _run(self) -> None:
        settings = self._settings
        if settings is None:
            raise RuntimeError("Telegram worker started without settings")
        heartbeat_at = time.monotonic() + settings.telegram_heartbeat_seconds
        next_send_at = 0.0
        try:
            while True:
                if not self._stop.is_set() and time.monotonic() >= heartbeat_at:
                    snapshot = self.status()
                    self.publish("heartbeat")
                    logger.info(
                        "Telegram counters: sent=%s failed=%s dropped=%s suppressed=%s",
                        snapshot["sent"], snapshot["failed"], snapshot["dropped"], snapshot["suppressed"],
                    )
                    heartbeat_at = time.monotonic() + settings.telegram_heartbeat_seconds
                try:
                    created_at, text = self._queue.get(timeout=0.2)
                except queue.Empty:
                    if self._stop.is_set():
                        break
                    continue
                try:
                    delay = max(0.0, next_send_at - time.monotonic())
                    # Preserve pacing during graceful shutdown, too.
                    if delay:
                        time.sleep(delay)
                    if time.monotonic() - created_at > self.MAX_EVENT_AGE_SECONDS:
                        with self._lock:
                            self._dropped += 1
                            self._last_error = "Expired queued notification dropped"
                        logger.warning("Expired Telegram notification dropped")
                    else:
                        self._send(text)
                    next_send_at = time.monotonic() + self.SEND_INTERVAL_SECONDS
                finally:
                    self._queue.task_done()
                if self._stop.is_set():
                    # Flush at most the shutdown message, not minutes of stale backlog.
                    discarded = 0
                    while True:
                        try:
                            _, pending = self._queue.get_nowait()
                        except queue.Empty:
                            break
                        self._queue.task_done()
                        if EVENTS["shutdown"] in pending:
                            time.sleep(self.SEND_INTERVAL_SECONDS)
                            self._send(pending)
                        else:
                            discarded += 1
                    if discarded:
                        with self._lock:
                            self._dropped += discarded
                            self._last_error = "Pending notifications dropped during shutdown"
                        logger.warning("Telegram shutdown dropped %d pending notification(s)", discarded)
                    break
        except Exception:
            # A worker failure must be visible without exporting request/exception secrets.
            self._record_failure("Notification worker stopped unexpectedly; restart required")


notifier = TelegramNotifier()
