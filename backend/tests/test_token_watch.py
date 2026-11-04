"""Token-expiry alert rules; offline, synthetic tokens only."""
import base64
import json
import unittest
from datetime import datetime

from app.token_watch import IST, token_alerts, token_expiry


def jwt(exp: datetime) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"exp": int(exp.timestamp())}).encode()).decode().rstrip("=")
    return f"h.{body}.s"


class TokenWatchTests(unittest.TestCase):
    def test_expiry_decoded_and_garbage_is_none(self):
        exp = datetime(2026, 11, 6, 0, 56, tzinfo=IST)
        self.assertEqual(token_expiry(jwt(exp)), exp)
        self.assertIsNone(token_expiry("not-a-jwt"))

    def test_startup_evening_soon_and_expired_alerts_fire_once(self):
        sent = set()
        exp = datetime(2026, 11, 6, 0, 56, tzinfo=IST)          # Thu night
        tokens = {"master": jwt(exp), "child": jwt(exp)}
        morning = datetime(2026, 11, 5, 9, 0, tzinfo=IST)
        alerts = token_alerts(tokens, morning, sent, startup=True)
        self.assertEqual([(lvl, acc) for lvl, acc, _ in alerts], [("info", "master"), ("info", "child")])
        self.assertIn("valid till 06-Nov 00:56", alerts[0][2])
        self.assertEqual(token_alerts(tokens, morning, sent, startup=False), [])
        evening = datetime(2026, 11, 5, 15, 30, tzinfo=IST)
        alerts = token_alerts(tokens, evening, sent, startup=False)
        self.assertEqual({lvl for lvl, _, _ in alerts}, {"warn"})
        self.assertIn("before 9:00 AM", alerts[0][2])
        self.assertEqual(token_alerts(tokens, evening, sent, startup=False), [])
        soon = token_alerts(tokens, datetime(2026, 11, 6, 0, 10, tzinfo=IST), sent, startup=False)
        self.assertTrue(all(lvl == "error" and "46 min" in text for lvl, _, text in soon))
        dead = token_alerts(tokens, datetime(2026, 11, 6, 9, 0, tzinfo=IST), sent, startup=True)
        self.assertTrue(all(lvl == "error" and "EXPIRED" in text for lvl, _, text in dead))

    def test_token_valid_through_next_session_has_no_evening_warning(self):
        exp = datetime(2026, 11, 6, 15, 0, tzinfo=IST)
        alerts = token_alerts({"master": jwt(exp)}, datetime(2026, 11, 5, 15, 45, tzinfo=IST), set(), False)
        self.assertEqual(alerts, [])


if __name__ == "__main__":
    unittest.main()
