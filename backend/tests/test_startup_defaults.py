import asyncio
import logging
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app.config import Settings
from app.routers import trading_accounts


def settings(**kw):
    return Settings(_env_file=None, dhan_client_id="m", dhan_access_token="t",
                    dhan_child_client_id="c", dhan_child_access_token="u", **kw)


class StartupDefaultsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for target, kw in (("selection_lock", {"new": asyncio.Lock()}),
                           ("entry_accounts", {"new": ()})):
            p = patch.object(trading_accounts.engines, target, **kw)
            p.start()
            self.addCleanup(p.stop)

    def test_defaults_are_master_only_live_fast_and_auto_arm(self):
        s = Settings(_env_file=None)
        self.assertEqual(s.trading_default_accounts, ["master"])
        self.assertTrue(s.algo_auto_arm)
        self.assertFalse(s.app_dry_run)
        self.assertTrue(s.app_fast_orders)
        self.assertEqual(settings(TRADING_DEFAULT_ACCOUNTS=" Master, child ").trading_default_accounts,
                         ["master", "child"])

    async def test_selects_without_arming_when_auto_arm_off(self):
        s = settings(TRADING_DEFAULT_ACCOUNTS="master,child", algo_auto_arm=False)
        with patch.object(trading_accounts, "get_settings", return_value=s), \
             patch.object(trading_accounts, "select_accounts") as select, \
             patch.object(trading_accounts, "algo_action", new=AsyncMock()) as arm:
            result = await trading_accounts.apply_startup_defaults()
        select.assert_called_once_with(["master", "child"])
        arm.assert_not_called()
        self.assertEqual(result, {"ok": True, "armed": False})

    async def test_auto_arm_uses_the_guarded_shared_arm(self):
        s = settings(TRADING_DEFAULT_ACCOUNTS="master,child", algo_auto_arm=True)
        async def fake_arm(body):
            self.assertEqual(body.action, "arm")
            trading_accounts.engines.entry_accounts = ("master", "child")
            return {"ok": True, "armed": True, "results": []}
        with patch.object(trading_accounts, "get_settings", return_value=s), \
             patch.object(trading_accounts, "select_accounts"), \
             patch.object(trading_accounts, "algo_action", new=AsyncMock(side_effect=fake_arm)), \
             self.assertLogs("dhan.accounts", logging.INFO) as logs:
            result = await trading_accounts.apply_startup_defaults()
        self.assertTrue(result["ok"])
        self.assertEqual(logs.records[-1].telegram_event, "auto_armed")

    async def test_partial_or_refused_arm_alerts_failure(self):
        s = settings(TRADING_DEFAULT_ACCOUNTS="master,child", algo_auto_arm=True)
        for outcome in ({"ok": False, "results": [{"accountId": "child", "ok": False}]},
                        HTTPException(409, "A position is already tracked")):
            with patch.object(trading_accounts, "get_settings", return_value=s), \
                 patch.object(trading_accounts, "select_accounts"), \
                 patch.object(trading_accounts, "algo_action", new=AsyncMock(side_effect=[outcome])), \
                 self.assertLogs("dhan.accounts", logging.ERROR) as logs:
                result = await trading_accounts.apply_startup_defaults()
            self.assertFalse(result["ok"])
            self.assertEqual(logs.records[-1].telegram_event, "auto_arm_failed")

    async def test_invalid_selection_never_arms(self):
        s = settings(TRADING_DEFAULT_ACCOUNTS="master,bogus", algo_auto_arm=True)
        with patch.object(trading_accounts, "get_settings", return_value=s), \
             patch.object(trading_accounts, "algo_action", new=AsyncMock()) as arm, \
             self.assertLogs("dhan.accounts", logging.ERROR) as logs:
            result = await trading_accounts.apply_startup_defaults()
        arm.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertEqual(logs.records[-1].telegram_event, "auto_arm_failed")


if __name__ == "__main__":
    unittest.main()
