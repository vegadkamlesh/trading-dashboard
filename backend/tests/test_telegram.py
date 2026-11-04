"""Offline notification/privacy tests; never contact Telegram or Dhan."""
import io
import json
import logging
import time
import unittest
from urllib.error import HTTPError, URLError
from unittest.mock import MagicMock, patch

import httpx
from fastapi import FastAPI
from pydantic import ValidationError

from app.config import Settings
from app.security import CSRF_HEADER, SESSION_COOKIE, SlidingWindowLimiter, create_session
from app.telegram_notify import EVENTS, NotificationHandler, TelegramNotifier


def configured(**overrides):
    values = {
        "_env_file": None,
        "telegram_enabled": True,
        "telegram_bot_token": "123456:offline-test-token",
        "telegram_chat_id": "-100123456",
        "app_session_secret": "offline-test-session",
    }
    values.update(overrides)
    return Settings(**values)


class TelegramTests(unittest.TestCase):
    def service(self):
        service = TelegramNotifier()
        service._settings = configured()
        return service

    def response(self, body):
        return io.BytesIO(json.dumps(body).encode())

    def test_opt_in_and_invalid_configuration(self):
        service = TelegramNotifier()
        service.start(configured(telegram_enabled=False))
        self.assertFalse(service.status()["running"])
        self.assertFalse(service.publish("test"))
        for values in (
            {"telegram_bot_token": ""},
            {"telegram_chat_id": ""},
            {"telegram_bot_token": "https://unexpected.example"},
            {"telegram_chat_id": "arbitrary destination"},
            {"telegram_heartbeat_seconds": 59},
        ):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                configured(**values)
        self.assertNotIn("offline-test-token", repr(configured()))
        self.assertNotIn("-100123456", repr(configured()))

    def test_api_confirmation_and_no_sensitive_status(self):
        service = self.service()
        with patch("app.telegram_notify.urlopen", return_value=self.response({"ok": True})) as send:
            self.assertTrue(service._send("safe fixed label"))
        request = send.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data)["text"], "safe fixed label")
        self.assertNotIn("allow_paid_broadcast", json.loads(request.data))
        status = service.status()
        self.assertEqual(status["sent"], 1)
        self.assertIsNotNone(status["lastSentAt"])
        self.assertNotIn("offline-test-token", json.dumps(status))
        self.assertNotIn("-100123456", json.dumps(status))

    def test_unconfirmed_and_invalid_responses_are_failures(self):
        for body in (b'{"ok":false}', b"[]", b"not-json"):
            service = self.service()
            with self.subTest(body=body), patch("app.telegram_notify.urlopen", return_value=io.BytesIO(body)):
                with self.assertLogs("dhan.telegram", level="ERROR"):
                    self.assertFalse(service._send("test"))
            self.assertEqual(service.status()["failed"], 1)
            self.assertEqual(service.status()["sent"], 0)

    def test_http_403_not_retried_or_leaked(self):
        service = self.service()
        error = HTTPError(
            "https://api.telegram.org/bot123456:offline-test-token/sendMessage",
            403, "private upstream secret", {}, io.BytesIO(b'{"description":"private broker data"}'),
        )
        with patch("app.telegram_notify.urlopen", side_effect=error) as send:
            with self.assertLogs("dhan.telegram", level="ERROR") as logs:
                self.assertFalse(service._send("test"))
        self.assertEqual(send.call_count, 1)
        self.assertIn("HTTP 403", service.status()["lastError"])
        for secret in ("offline-test-token", "private upstream secret", "private broker data"):
            self.assertNotIn(secret, "\n".join(logs.output))

    def test_rate_limit_respects_retry_after(self):
        service = self.service()
        error = HTTPError("redacted", 429, "rate limit", {},
                          io.BytesIO(b'{"parameters":{"retry_after":7}}'))
        with patch("app.telegram_notify.urlopen", side_effect=[error, self.response({"ok": True})]):
            with patch.object(service._stop, "wait", return_value=False) as wait:
                self.assertTrue(service._send("test"))
        wait.assert_called_once_with(7)

    def test_rate_limit_beyond_window_fails_explicitly(self):
        service = self.service()
        error = HTTPError("redacted", 429, "rate limit", {},
                          io.BytesIO(b'{"parameters":{"retry_after":120}}'))
        with patch("app.telegram_notify.urlopen", side_effect=error) as send:
            with self.assertLogs("dhan.telegram", level="ERROR"):
                self.assertFalse(service._send("test"))
        self.assertEqual(send.call_count, 1)
        self.assertIn("retry window", service.status()["lastError"])

    def test_network_retry_bounded_and_credentials_redacted(self):
        service = self.service()
        with patch("app.telegram_notify.urlopen", side_effect=URLError("offline-test-token")) as send:
            with patch.object(service._stop, "wait", return_value=False) as wait:
                with self.assertLogs("dhan.telegram", level="ERROR") as logs:
                    self.assertFalse(service._send("test"))
        self.assertEqual(send.call_count, 3)
        self.assertEqual(wait.call_count, 2)
        self.assertTrue(all(call.args[0] >= 3.1 for call in wait.call_args_list))
        self.assertNotIn("offline-test-token", "\n".join(logs.output))

    def test_algo_lines_carry_short_detail_other_records_stay_metadata_only(self):
        service = self.service()
        service._thread = MagicMock()
        service._thread.is_alive.return_value = True
        handler = NotificationHandler(service)

        def record(name, level, msg, **extra):
            rec = logging.LogRecord(name, level, "", 0, msg, None, None)
            for key, value in extra.items():
                setattr(rec, key, value)
            handler.emit(rec)
            return service._queue.get_nowait()[1]

        entry = record("dhan.algo", logging.INFO, "x", telegram_event="algo_entry",
                       telegram_account="child", telegram_mode="LIVE", telegram_level="success",
                       telegram_detail="Entry confirmed: SENSEX 76800 PE 100 units @ 363.55")
        self.assertIn("✅", entry)
        self.assertIn("child LIVE", entry)
        self.assertIn("76800 PE 100 units @ 363.55", entry)
        reject = record("dhan.algo", logging.ERROR, "x", telegram_event="algo_update",
                        telegram_account="child", telegram_mode="LIVE", telegram_level="error",
                        telegram_detail="Entry REJECTED: insufficient funds; ACTION: check Dhan")
        self.assertIn("🚨", reject)
        self.assertIn("insufficient funds", reject)
        other = record("dhan.client", logging.WARNING, "private broker payload")
        self.assertNotIn("private broker payload", other)
        self.assertIn(EVENTS["warning"], other)

    def test_burst_of_events_is_sent_as_one_message(self):
        service = self.service()
        for i in range(3):
            service._queue.put_nowait((time.monotonic(), f"line-{i}"))
        sent = []
        with patch.object(service, "_send", side_effect=lambda text: sent.append(text) or True):
            import threading
            worker = threading.Thread(target=service._run)
            worker.start()
            service._queue.join()
            service._stop.set()
            worker.join(timeout=5)
        self.assertEqual(sent, ["line-0\n\nline-1\n\nline-2"])

    def test_worker_is_nonblocking_idempotent_and_stops(self):
        service = TelegramNotifier()
        sent_at = []

        def deliver(*args, **kwargs):
            sent_at.append(time.monotonic())
            return self.response({"ok": True})

        self.enterContext(patch("app.telegram_notify.urlopen",
                               side_effect=deliver))
        self.addCleanup(service.stop)
        service.start(configured())
        worker = service._thread
        service.start(configured())
        self.assertIs(service._thread, worker)
        self.assertTrue(service.publish("test"))
        service._queue.join()
        # startup + test were queued together, so they go out as one batched message.
        self.assertEqual(service.status()["sent"], 1)
        service.stop()
        self.assertEqual(len(sent_at), 2)
        self.assertTrue(all(second - first >= 3.1 for first, second in zip(sent_at, sent_at[1:])))
        self.assertFalse(service.status()["running"])
        self.assertFalse(service.publish("test"))
        self.assertNotIn(service._handler, logging.getLogger().handlers)

    def test_heartbeat_has_counters_but_no_broker_readiness_claim(self):
        service = self.service()
        service._thread = MagicMock()
        service._thread.is_alive.return_value = True
        service._failed = 2
        self.assertTrue(service.publish("heartbeat"))
        _, text = service._queue.get_nowait()
        service._queue.task_done()
        self.assertIn("failed=2", text)
        self.assertIn("not broker/trading readiness", text)

    def test_handler_never_exports_messages_arguments_or_tracebacks(self):
        service = self.service()
        service._thread = MagicMock()
        service._thread.is_alive.return_value = True
        handler = NotificationHandler(service)
        record = logging.LogRecord("dhan.algo", logging.ERROR, "", 1,
                                   "secret token=%s", ("private-broker-token",), None)
        record.telegram_event = "algo_entry"
        record.telegram_account = "master"
        record.telegram_mode = "LIVE"
        handler.emit(record)
        _, text = service._queue.get_nowait()
        service._queue.task_done()
        self.assertIn(EVENTS["algo_entry"], text)
        self.assertIn("Account: master", text)
        self.assertIn("Mode: LIVE", text)
        self.assertNotIn("private-broker-token", text)
        self.assertNotIn("secret", text)
        record.telegram_event = "not-allowlisted"
        record.telegram_account = "private-client-id"
        record.telegram_mode = "private balance"
        handler.emit(record)
        _, text = service._queue.get_nowait()
        service._queue.task_done()
        self.assertIn(EVENTS["error"], text)
        self.assertNotIn("private", text)
        record.name = "dhan.telegram"
        handler.emit(record)
        self.assertTrue(service._queue.empty())
        record.name = "dhan.access"
        record.levelno = logging.INFO
        record.telegram_event = None
        handler.emit(record)
        self.assertTrue(service._queue.empty())

    def test_generic_errors_grouped_but_trade_events_not_grouped(self):
        service = self.service()
        service._thread = MagicMock()
        service._thread.is_alive.return_value = True
        self.assertTrue(service.publish("error", account="master"))
        self.assertFalse(service.publish("error", account="master"))
        self.assertTrue(service.publish("error", account="child"))
        self.assertTrue(service.publish("algo_exit"))
        self.assertTrue(service.publish("algo_exit"))
        self.assertEqual(service.status()["suppressed"], 1)

    def test_full_queue_is_explicit_and_does_not_wait_for_network(self):
        service = self.service()
        service._thread = MagicMock()
        service._thread.is_alive.return_value = True
        for _ in range(service.QUEUE_SIZE):
            self.assertTrue(service.publish("test"))
        started = time.monotonic()
        with self.assertLogs("dhan.telegram", level="ERROR"):
            self.assertFalse(service.publish("test"))
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(service.status()["dropped"], 1)
        self.assertEqual(service.status()["queued"], service.QUEUE_SIZE)

    def test_expired_events_are_not_sent(self):
        service = self.service()
        service.SEND_INTERVAL_SECONDS = 0
        service._queue.put((time.monotonic() - 601, "stale"))
        service._stop.set()
        with patch.object(service, "_send") as send:
            with self.assertLogs("dhan.telegram", level="WARNING"):
                service._run()
        send.assert_not_called()
        self.assertEqual(service.status()["dropped"], 1)

    def test_worker_unexpected_failure_visible_and_publish_rejected(self):
        service = self.service()
        with patch.object(service._queue, "get", side_effect=RuntimeError("private secret")):
            with self.assertLogs("dhan.telegram", level="ERROR") as logs:
                service._run()
        self.assertFalse(service.publish("test"))
        self.assertEqual(service.status()["failed"], 1)
        self.assertNotIn("private secret", "\n".join(logs.output))


class TelegramRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_routes_require_auth_csrf_and_report_queued_not_delivered(self):
        from app.routers import settings as routes

        app = FastAPI()
        app.include_router(routes.router)
        notifier = MagicMock()
        notifier.status.return_value = {"running": True, "enabled": True, "sent": 0}
        notifier.publish.return_value = True
        with patch.object(routes, "notifier", notifier):
            with patch("app.security.get_settings", return_value=configured()):
                with patch("app.security.limiter", SlidingWindowLimiter()):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                                 base_url="http://test") as client:
                        self.assertEqual((await client.get("/api/settings/telegram")).status_code, 401)
                        self.assertEqual((await client.post("/api/settings/telegram/test")).status_code, 401)
                        token = create_session()
                        client.cookies.set(SESSION_COOKIE, token)
                        self.assertEqual((await client.post("/api/settings/telegram/test")).status_code, 403)
                        response = await client.post("/api/settings/telegram/test", headers={CSRF_HEADER: token})
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(response.json()["delivery"], "queued")
                        status = await client.get("/api/settings/telegram")
                        self.assertEqual(status.json()["sent"], 0)
                        notifier.publish.return_value = False
                        response = await client.post("/api/settings/telegram/test", headers={CSRF_HEADER: token})
                        self.assertEqual(response.status_code, 503)
                        notifier.status.return_value = {"running": False}
                        response = await client.post("/api/settings/telegram/test", headers={CSRF_HEADER: token})
                        self.assertEqual(response.status_code, 409)
                        response = await client.post("/api/settings/telegram/test", headers={CSRF_HEADER: token})
                        self.assertEqual(response.status_code, 429)


if __name__ == "__main__":
    unittest.main()
