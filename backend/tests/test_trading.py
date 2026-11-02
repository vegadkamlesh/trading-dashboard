import asyncio
import importlib
import struct
import time
import unittest
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

algo_module = importlib.import_module("app.algo")
from app.algo import AlgoEngine, DayLeg, Position
from app.feed import MarketFeed
from app.instruments import get_index
from app.dhan_client import DhanAPIError, DhanClient
from app.rate_limit import RateGate, gate_for, DATA_GATE, QUOTE_GATE, ORDER_GATE


def position(**changes):
    values = dict(
        index_key="NIFTY", option_type="CALL", security_id=47153, strike=22800,
        expiry="2026-10-19", lots=1, quantity=65, entry_spot=22752.3,
        entry_premium=127.85, entry_time="09:47:07", entry_ts=1.0,
        or_range=88.5, target_spot=22929.3, stop_spot=22734.6,
        initial_stop_spot=22734.6, dry_run=False, spot=22752.3, premium=127.65,
        order_id="entry-1",
    )
    return Position(**(values | changes))


class AlgoRegressionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store_patch = patch.object(algo_module, "store", MagicMock())
        self.store_patch.start()
        self.addCleanup(self.store_patch.stop)
        self.exits_read_patch = patch.object(algo_module.audit, "pending_manual_exits", return_value={})
        self.exits_read_patch.start()
        self.addCleanup(self.exits_read_patch.stop)
        self.exits_write_patch = patch.object(algo_module.audit, "save_manual_exit")
        self.exits_write_patch.start()
        self.addCleanup(self.exits_write_patch.stop)
        request_patch = patch.object(algo_module.audit, "read_manual_request", return_value=(False, None))
        request_patch.start()
        self.addCleanup(request_patch.stop)
        self.client = MagicMock()
        self.client.get_positions = AsyncMock(return_value=[{
            "securityId": "47153", "netQty": 65,
            "exchangeSegment": "NSE_FNO", "productType": "INTRADAY",
        }])
        self.client.get_order_by_id = AsyncMock()
        self.client.get_order_by_correlation = AsyncMock()
        self.client.get_order_book = AsyncMock(return_value=[])
        self.client.get_super_orders = AsyncMock(return_value=[])
        self.client.cancel_order = AsyncMock()
        self.client.place_order = AsyncMock(return_value={"orderId": "exit-1", "orderStatus": "TRANSIT"})
        self.client_patch = patch.object(algo_module, "get_dhan_client", return_value=self.client)
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)
        self.network_guard = patch("httpx.AsyncClient.request", side_effect=AssertionError("Tests must never call the network"))
        self.network_guard.start()
        self.addCleanup(self.network_guard.stop)
        self.engine = AlgoEngine()
        self.engine._day = "2026-10-14"
        self.engine._position = position()
        self.engine._legs = {"NIFTY": DayLeg("NIFTY", status="active")}
        self.engine.cfg.protective_sl = False
        self.engine.cfg.target_only = False
        self.settings_patch = patch.object(algo_module, "get_settings", return_value=MagicMock(
            is_configured=MagicMock(return_value=True), dhan_client_id="test", algo_paper_delta=.5,
        ))
        self.settings_patch.start()
        self.addCleanup(self.settings_patch.stop)

    async def test_index_tick_updates_open_position_spot(self):
        self.engine._on_tick(get_index("NIFTY").sec_id, 22740.0)
        self.assertEqual(self.engine._position.spot, 22740.0)
        self.assertAlmostEqual(self.engine._position.pnl_points, -12.3)

    async def test_old_day_flat_position_with_expired_order_lookup_recovers(self):
        self.client.get_positions.return_value = []
        self.client.get_order_by_id.return_value = []
        self.engine._position.protective_order_id = "old-sl"
        await self.engine.reconcile()
        self.assertIsNone(self.engine._position)
        self.client.cancel_order.assert_not_awaited()
        self.client.place_order.assert_not_awaited()
        self.assertTrue(self.engine._today_trades[0]["exitPriceEstimated"])

    async def test_same_day_empty_order_lookup_never_discards_position(self):
        self.engine._day = algo_module.now_ist().date().isoformat()
        self.client.get_positions.return_value = []
        self.client.get_order_by_id.return_value = []
        self.engine._position.protective_order_id = "current-sl"
        await self.engine.reconcile()
        self.assertIsNotNone(self.engine._position)

    async def test_historical_recovery_blocks_active_or_unknown_current_order(self):
        for status in ("PENDING", ""):
            self.client.get_positions.return_value = []
            self.client.get_order_by_id.return_value = []
            self.engine._position.protective_order_id = "old-sl"
            self.client.get_order_book.return_value = [{
                "securityId": "47153", "exchangeSegment": "NSE_FNO",
                "productType": "INTRADAY", "orderStatus": status,
            }]
            await self.engine.reconcile()
            self.assertIsNotNone(self.engine._position)

    async def test_historical_recovery_requires_fresh_flat_recheck(self):
        self.client.get_positions.side_effect = [[], [{
            "securityId": "47153", "exchangeSegment": "NSE_FNO",
            "productType": "INTRADAY", "netQty": 65,
        }]]
        self.client.get_order_by_id.return_value = []
        self.engine._position.protective_order_id = "old-sl"
        await self.engine.reconcile()
        self.assertIsNotNone(self.engine._position)

    async def test_switch_to_live_reconciles_old_flat_state_without_sending_order(self):
        from app.routers import settings
        self.client.get_positions.return_value = []
        self.client.get_order_by_id.return_value = []
        self.engine._position.protective_order_id = "old-sl"
        with patch.object(settings, "algo", self.engine), \
                patch.object(settings.runtime, "is_dry_run", return_value=True), \
                patch.object(settings.runtime, "set_dry_run", return_value=False) as switch:
            result = await settings.set_dry_run(settings.ToggleBody(value=False), "test", None)
        switch.assert_called_once_with(False)
        self.assertFalse(result["dryRun"])
        self.assertFalse(self.engine.enabled)
        self.client.place_order.assert_not_awaited()

    async def test_mode_switch_keeps_current_position_guard_and_surfaces_detail(self):
        from app.routers import settings
        from fastapi import HTTPException
        self.engine._day = algo_module.now_ist().date().isoformat()
        self.engine._position.execution_error = "Protective cancellation not confirmed"
        with patch.object(settings, "algo", self.engine), \
                patch.object(settings.runtime, "is_dry_run", return_value=True), \
                patch.object(settings.runtime, "set_dry_run") as switch:
            with self.assertRaises(HTTPException) as caught:
                await settings.set_dry_run(settings.ToggleBody(value=False), "test", None)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn("Protective cancellation not confirmed", caught.exception.detail)
        switch.assert_not_called()
        self.client.cancel_order.assert_not_awaited()

    async def test_historical_recovery_never_drops_unknown_super_exit_leg(self):
        self.client.get_positions.return_value = []
        self.client.get_super_orders.return_value = [{
            "securityId": "47153", "exchangeSegment": "NSE_FNO", "productType": "INTRADAY",
            "orderStatus": "TRADED", "legDetails": [{"orderStatus": "PENDING"}],
        }]
        await self.engine.reconcile()
        self.assertIsNotNone(self.engine._position)
        self.client.cancel_order.assert_not_awaited()

    async def test_historical_recovery_preserves_tracking_if_broker_books_fail(self):
        self.client.get_positions.return_value = []
        self.client.get_order_book.side_effect = DhanAPIError(429, "DH-904", "Rate limited")
        await self.engine.reconcile()
        self.assertIsNotNone(self.engine._position)
        self.assertEqual(self.engine._broker_sync_error, "Rate limited")
        self.client.place_order.assert_not_awaited()

    async def test_historical_recovery_does_not_clear_carry_or_same_day_pending_entry(self):
        self.client.get_positions.return_value = []
        pos = self.engine._position
        self.engine.cfg.product_type = "MARGIN"
        self.assertFalse(await self.engine._recover_historical_flat(pos))
        self.engine.cfg.product_type = "INTRADAY"
        self.engine._day = algo_module.now_ist().date().isoformat()
        pos.execution_state = "entry_pending"
        self.assertFalse(await self.engine._recover_historical_flat(pos))
        self.assertIs(self.engine._position, pos)
        self.client.get_order_book.assert_not_awaited()

    async def test_prior_day_unknown_entry_recovers_flat_without_inventing_fill_or_pnl(self):
        self.engine._position.execution_state = "entry_pending"
        self.client.get_positions.return_value = []
        self.client.get_order_by_id.return_value = []
        await self.engine.reconcile()
        self.assertIsNone(self.engine._position)
        self.assertEqual(self.engine._today_trades, [])
        self.client.cancel_order.assert_not_awaited()
        self.client.place_order.assert_not_awaited()

    async def test_prior_day_pending_entry_with_current_order_remains_tracked(self):
        self.engine._position.execution_state = "entry_pending"
        self.client.get_positions.return_value = []
        self.client.get_order_by_id.return_value = []
        self.client.get_order_book.return_value = [{
            "securityId": "47153", "exchangeSegment": "NSE_FNO", "productType": "INTRADAY",
            "orderStatus": "PENDING", "orderId": "entry-1",
        }]
        await self.engine.reconcile()
        self.assertIsNotNone(self.engine._position)
        self.client.place_order.assert_not_awaited()

    async def test_list_order_detail_opens_confirmed_entry_not_pending(self):
        self.engine._day = algo_module.now_ist().date().isoformat()
        pos = self.engine._position
        pos.execution_state = "entry_pending"
        self.client.get_order_by_id.return_value = [{
            "orderId": pos.order_id, "orderStatus": "TRADED", "quantity": 65,
            "filledQty": 65, "averageTradedPrice": 100,
        }]
        await self.engine._refresh_entry(pos)
        self.assertEqual(pos.execution_state, "open")
        self.assertEqual(pos.entry_premium, 100)

    async def test_external_flat_reports_broker_zero_even_if_protection_unknown(self):
        self.engine._day = algo_module.now_ist().date().isoformat()
        pos = self.engine._position
        pos.protective_order_id = "missing-sl"
        self.client.get_order_by_id.return_value = []
        self.client.get_positions.return_value = []
        await self.engine.reconcile()
        self.assertIs(self.engine._position, pos)
        self.assertEqual(self.engine._broker_net_qty, 0)
        self.assertEqual(pos.execution_state, "attention")
        self.client.place_order.assert_not_awaited()

    async def test_algo_pnl_uses_broker_contract_values_not_quote_estimate(self):
        self.client.get_positions.return_value[0].update(realizedProfit=12, unrealizedProfit=35)
        await self.engine.reconcile()
        self.assertEqual(self.engine._broker_position_pnl, 47)
        self.assertEqual(self.engine.status()["brokerPositionPnl"], 47)

    async def test_invalid_broker_pnl_is_explicit_without_losing_holding_quantity(self):
        self.client.get_positions.return_value[0].update(realizedProfit="bad", unrealizedProfit=35)
        await self.engine.reconcile()
        self.assertIsNone(self.engine._broker_position_pnl)
        self.assertEqual(self.engine._broker_net_qty, 65)
        self.assertIn("Invalid broker contract P&L", self.engine._broker_sync_error)

    async def test_other_exchange_tick_cannot_update_algo_premium(self):
        pos = self.engine._position
        original = pos.premium
        self.engine._on_tick(pos.security_id, 999, "BSE_FNO")
        self.assertEqual(pos.premium, original)
        self.engine._on_tick(pos.security_id, 150, "NSE_FNO")
        self.assertEqual(pos.premium, 150)

    async def test_derivative_id_matching_index_id_does_not_change_spot(self):
        pos = self.engine._position
        pos.security_id = get_index("NIFTY").sec_id
        self.engine._on_tick(pos.security_id, 100, "NSE_FNO")
        self.assertEqual(pos.premium, 100)
        self.assertEqual(pos.spot, 22752.3)

    async def test_float32_boundary_noise_is_not_a_breakout(self):
        self.engine._position = None
        self.engine._legs = {"NIFTY": DayLeg("NIFTY", status="watching",
                                           or_high=22752.3, or_low=22663.8)}
        self.engine._on_tick(get_index("NIFTY").sec_id, 22752.30078125)
        with patch.object(algo_module.feed, "age", return_value=0), \
                patch.object(algo_module, "now_ist", return_value=algo_module.datetime(2026, 10, 14, 10, tzinfo=algo_module.IST)), \
                patch.object(self.engine, "_enter", AsyncMock()) as entry:
            await self.engine._look_for_entry()
        self.assertEqual(self.engine._legs["NIFTY"].spot, 22752.3)
        entry.assert_not_awaited()

    async def test_finalise_clears_position_and_is_idempotent(self):
        pos = self.engine._position
        self.engine._finalise_position(pos, 130.0, "TARGET")
        self.engine._finalise_position(pos, 130.0, "TARGET")
        self.assertIsNone(self.engine._position)
        self.assertEqual(len(self.engine._today_trades), 1)
        self.assertEqual(self.engine._legs["NIFTY"].status, "closed")

    async def test_rejected_exit_does_not_claim_position_closed(self):
        pos = self.engine._position
        self.engine._send_order = AsyncMock(return_value={"ok": False, "error": "rejected"})
        await self.engine._exit_position(pos, "STOP")
        self.assertIs(self.engine._position, pos)
        self.assertEqual(self.engine._today_trades, [])

    async def test_pending_exit_is_not_sent_twice(self):
        pos = self.engine._position
        with patch.object(algo_module.audit, "log_order"):
            await asyncio.gather(
                self.engine._exit_position(pos, "STOP"),
                self.engine._exit_position(pos, "PANIC"),
            )
        self.client.place_order.assert_awaited_once()
        self.assertEqual(pos.execution_state, "exit_pending")
        self.assertIs(self.engine._position, pos)
        self.assertEqual(self.engine._today_trades, [])

    async def test_pending_entry_is_not_treated_as_filled(self):
        self.engine._day = algo_module.now_ist().date().isoformat()
        pos = self.engine._position
        pos.execution_state = "entry_pending"
        self.client.get_order_by_id.return_value = {"orderStatus": "PENDING", "orderId": "entry-1"}
        with patch.object(self.engine, "_place_protective_sl", AsyncMock()) as protective:
            await self.engine.reconcile()
        self.assertEqual(pos.execution_state, "entry_pending")
        protective.assert_not_awaited()
        self.client.get_positions.assert_not_awaited()

    async def test_rejected_entry_never_creates_protective_sell(self):
        pos = self.engine._position
        pos.execution_state = "entry_pending"
        self.client.get_order_by_id.return_value = {"orderStatus": "REJECTED", "filledQty": 0}
        with patch.object(self.engine, "_place_protective_sl", AsyncMock()) as protective:
            await self.engine.reconcile()
        self.assertIsNone(self.engine._position)
        protective.assert_not_awaited()
        self.assertEqual(self.engine._today_trades, [])

    async def test_confirmed_entry_sets_propagation_grace(self):
        pos = self.engine._position
        pos.execution_state = "entry_pending"
        self.client.get_order_by_id.return_value = {
            "orderStatus": "TRADED", "filledQty": 65, "averageTradedPrice": 128,
        }
        self.client.get_positions.return_value = []
        with patch.object(algo_module.feed, "price", return_value=128):
            await self.engine._refresh_entry(pos)
            self.assertGreater(pos.entry_confirmed_ts, time.time() - 1)
            await self.engine.reconcile()
        self.assertIs(self.engine._position, pos)

    async def test_rejected_protective_order_marks_attention(self):
        pos = self.engine._position
        self.engine.cfg.protective_sl = True
        pos.protective_order_id = "sl-1"
        self.client.get_order_by_id.return_value = {"orderStatus": "REJECTED"}
        await self.engine.reconcile()
        self.assertEqual(pos.execution_state, "attention")

    async def test_partial_entry_waits_for_cancel_before_protection(self):
        pos = self.engine._position
        pos.execution_state = "entry_pending"
        self.client.get_order_by_id.side_effect = [
            {"orderStatus": "PART_TRADED", "filledQty": 30, "averageTradedPrice": 128},
            {"orderStatus": "CANCELLED", "filledQty": 30, "averageTradedPrice": 128},
        ]
        self.engine.cfg.protective_sl = True
        with patch.object(self.engine, "_place_protective_sl", AsyncMock()) as protective:
            await self.engine._refresh_entry(pos)
            self.assertEqual(pos.execution_state, "entry_pending")
            protective.assert_not_awaited()
            await self.engine._refresh_entry(pos)
            protective.assert_awaited_once()
        self.assertEqual(pos.quantity, 30)
        self.assertEqual(pos.entry_premium, 128)

    async def test_paper_position_does_not_reconcile_against_live(self):
        self.engine._position.dry_run = True
        self.client.get_positions.return_value = []
        await self.engine.reconcile()
        self.assertIsNotNone(self.engine._position)
        self.client.get_positions.assert_not_awaited()

    async def test_broker_flat_finalises_even_when_disarmed(self):
        self.engine.enabled = False
        self.client.get_positions.return_value = [{
            "securityId": "47153", "netQty": 0, "sellAvg": 130,
            "exchangeSegment": "NSE_FNO", "productType": "INTRADAY",
        }]
        with patch.object(algo_module, "now_ist", return_value=algo_module.datetime(2026, 10, 14, 10, tzinfo=algo_module.IST)):
            await self.engine._tick_supervisor()
        self.assertIsNone(self.engine._position)
        self.assertEqual(self.engine._today_trades[0]["exitPremium"], 130)
        self.client.place_order.assert_not_awaited()

    async def test_broker_error_preserves_tracking(self):
        pos = self.engine._position
        self.client.get_positions.side_effect = DhanAPIError(429, "DH-904", "Too many requests")
        await self.engine.reconcile()
        self.assertIs(self.engine._position, pos)
        self.assertEqual(self.engine._broker_sync_error, "Too many requests")

    async def test_stop_fill_during_cancel_never_sends_second_sell(self):
        pos = self.engine._position
        pos.protective_order_id = "sl-1"
        self.client.get_order_by_id.side_effect = [
            {"orderStatus": "PENDING", "orderId": "sl-1"},
            {"orderStatus": "TRADED", "orderId": "sl-1", "averageTradedPrice": 110},
            {"orderStatus": "TRADED", "orderId": "sl-1", "averageTradedPrice": 110},
        ]
        self.client.get_positions.return_value = []
        await self.engine._exit_position(pos, "STOP")
        self.client.place_order.assert_not_awaited()
        self.assertIsNone(self.engine._position)
        self.assertEqual(self.engine._today_trades[0]["exitPremium"], 110)

    async def test_stop_fill_with_lagging_holdings_cannot_sell_again(self):
        pos = self.engine._position
        pos.protective_order_id = "sl-1"
        self.client.get_order_by_id.return_value = {
            "orderStatus": "TRADED", "orderId": "sl-1", "averageTradedPrice": 110,
        }
        await self.engine._exit_position(pos, "STOP")
        await self.engine._exit_position(pos, "PANIC")
        self.client.place_order.assert_not_awaited()
        self.assertIs(self.engine._position, pos)
        self.assertEqual(pos.execution_state, "exit_pending")

    async def test_unconfirmed_stop_cancel_blocks_market_exit(self):
        pos = self.engine._position
        pos.protective_order_id = "sl-1"
        self.client.get_order_by_id.return_value = {"orderStatus": "PENDING", "orderId": "sl-1"}
        await self.engine._exit_position(pos, "PANIC")
        self.client.place_order.assert_not_awaited()
        self.assertIs(self.engine._position, pos)
        self.assertIn("cancellation not confirmed", pos.execution_error)

    async def test_timeout_exit_keeps_identity_and_blocks_replay(self):
        pos = self.engine._position
        self.client.place_order.side_effect = DhanAPIError(0, "DH-909", "Timed out")
        with patch.object(algo_module.audit, "log_order"):
            await self.engine._exit_position(pos, "STOP")
            await self.engine._exit_position(pos, "STOP")
        self.client.place_order.assert_awaited_once()
        self.assertIsNotNone(pos.exit_correlation_id)
        self.assertEqual(pos.execution_state, "exit_pending")

    async def test_call_and_put_stop_thresholds(self):
        for side, safe_spot, stopped_spot in [("CALL", 100, 95), ("PUT", 100, 105)]:
            pos = position(option_type=side, entry_spot=100, spot=safe_spot,
                           stop_spot=95 if side == "CALL" else 105,
                           target_spot=120 if side == "CALL" else 80)
            self.engine._position = pos
            with patch.object(algo_module.feed, "age", return_value=0), \
                    patch.object(self.engine, "_exit_position_locked", AsyncMock()) as exit_mock:
                await self.engine._manage_position()
                exit_mock.assert_not_awaited()
                pos.spot = stopped_spot
                await self.engine._manage_position()
                exit_mock.assert_awaited_once_with(pos, "STOP")

    async def test_stale_spot_does_not_trigger_stop(self):
        self.engine._position.spot = 22000
        with patch.object(algo_module.feed, "age", return_value=6), \
                patch.object(self.engine, "_exit_position_locked", AsyncMock()) as exit_mock:
            await self.engine._manage_position()
            exit_mock.assert_not_awaited()

    async def test_live_position_exit_ignores_global_dry_run_change(self):
        with patch.object(algo_module.runtime, "is_dry_run", return_value=True), \
                patch.object(algo_module.audit, "log_order"):
            await self.engine._exit_position(self.engine._position, "PANIC")
        self.client.place_order.assert_awaited_once()

    async def test_day_rollover_does_not_forget_live_position(self):
        pos = self.engine._position
        with patch.object(algo_module, "now_ist", return_value=algo_module.datetime(2026, 10, 15, 9, tzinfo=algo_module.IST)), \
                patch.object(self.engine, "_exit_position_locked", AsyncMock()) as exit_mock:
            await self.engine._tick_supervisor()
        self.assertIs(self.engine._position, pos)
        exit_mock.assert_awaited_once_with(pos, "TIME")

    async def test_fresh_fill_waits_for_position_book_propagation(self):
        self.engine._position.entry_confirmed_ts = time.time()
        self.client.get_positions.return_value = []
        await self.engine.reconcile()
        self.assertIsNotNone(self.engine._position)

    async def test_entry_post_pending_does_not_park_sell(self):
        self.engine._position = None
        leg = DayLeg("NIFTY", status="watching", or_high=100, or_low=80,
                     spot=101, security_id=47153, strike=100, prepared_side="CALL",
                     option_ltp=128, expiry="2026-10-19")
        self.engine._legs = {"NIFTY": leg}
        self.client.get_positions.return_value = []
        self.client.get_order_by_id.return_value = {"orderStatus": "PENDING", "orderId": "entry-1"}
        self.engine._prepare_contract = AsyncMock()
        self.engine._size_position = AsyncMock(return_value={"lots": 1, "quantity": 65})
        self.engine._send_order = AsyncMock(return_value={"ok": True, "orderId": "entry-1"})
        with patch.object(algo_module.runtime, "is_dry_run", return_value=False), \
                patch.object(algo_module.feed, "price", return_value=128), \
                patch.object(algo_module.feed, "age", return_value=0), \
                patch.object(self.engine, "_place_protective_sl", AsyncMock()) as protective:
            await self.engine._enter(leg, "CALL")
        self.assertEqual(self.engine._position.execution_state, "entry_pending")
        protective.assert_not_awaited()
        self.engine._send_order.assert_awaited_once()
        self.assertEqual(self.engine._position.stop_spot, 97)
        self.assertEqual(self.engine._position.target_spot, 141)

    async def test_algo_entry_does_not_mix_with_pending_manual_entry(self):
        self.engine._position = None
        leg = DayLeg("NIFTY", status="watching", or_high=100, or_low=80,
                     spot=101, security_id=47153, strike=100, prepared_side="CALL")
        self.client.get_positions.return_value = []
        self.client.get_super_orders.return_value = [{
            "securityId": "47153", "exchangeSegment": "NSE_FNO",
            "productType": "INTRADAY", "orderStatus": "PENDING",
        }]
        with patch.object(self.engine, "_prepare_contract", AsyncMock()), \
                patch.object(self.engine, "_send_order", AsyncMock()) as send, \
                patch.object(algo_module.feed, "price", return_value=128), \
                patch.object(algo_module.feed, "age", return_value=0), \
                patch.object(algo_module.runtime, "is_dry_run", return_value=False):
            await self.engine._enter(leg, "CALL")
        send.assert_not_awaited()
        self.assertIn("pending", leg.note)

    async def test_config_update_rejects_zero_stop_without_mutation(self):
        from fastapi import HTTPException
        self.engine._position = None
        with self.assertRaises(HTTPException):
            self.engine.set_config({"sl_mult": 0})
        self.assertEqual(self.engine.cfg.sl_mult, .2)

    async def test_target_only_ignores_spot_stop_for_calls_and_puts(self):
        self.engine.cfg.target_only = True
        for side, spot in [("CALL", 90), ("PUT", 110)]:
            self.engine._position = position(option_type=side, entry_spot=100, spot=spot,
                                             stop_spot=95 if side == "CALL" else 105,
                                             target_spot=120 if side == "CALL" else 80)
            with patch.object(algo_module.feed, "age", return_value=0), \
                    patch.object(self.engine, "_exit_position_locked", AsyncMock()) as exit_mock:
                await self.engine._manage_position()
                exit_mock.assert_not_awaited()

    async def test_target_only_never_ratchets_break_even_but_still_exits_at_target(self):
        self.engine.cfg.target_only = True
        self.engine.cfg.breakeven_at = .5
        pos = position(entry_spot=100, spot=110, stop_spot=95, target_spot=120, or_range=10)
        self.engine._position = pos
        with patch.object(algo_module.feed, "age", return_value=0), \
                patch.object(self.engine, "_exit_position_locked", AsyncMock()) as exit_mock:
            await self.engine._manage_position()
            self.assertFalse(pos.breakeven_done)
            self.assertEqual(pos.stop_spot, 95)
            exit_mock.assert_not_awaited()
            pos.spot = 120
            await self.engine._manage_position()
            exit_mock.assert_awaited_once_with(pos, "TARGET")

    async def test_target_only_far_stop_uses_entry_premium_not_or_distance(self):
        self.engine.cfg.target_only = True
        self.engine.cfg.protective_sl = True
        for side in ("CALL", "PUT"):
            pos = position(option_type=side, entry_premium=100)
            self.assertEqual(self.engine._protective_trigger(pos), 10)
            with patch.object(algo_module.feed, "price", return_value=99), \
                    patch.object(algo_module.feed, "age", return_value=0), \
                    patch.object(self.engine, "_send_order", AsyncMock(return_value={
                        "ok": True, "dryRun": True, "orderId": "sl-far",
                    })) as send:
                pos.dry_run = True
                await self.engine._place_protective_sl(pos)
                self.assertEqual(send.await_args.kwargs["trigger"], 10)

    async def test_target_only_keeps_1512_time_exit(self):
        self.engine.cfg.target_only = True
        self.engine._last_reconcile_at = time.monotonic()
        with patch.object(algo_module, "now_ist", return_value=algo_module.datetime(2026, 10, 14, 15, 12, tzinfo=algo_module.IST)), \
                patch.object(self.engine, "_exit_position_locked", AsyncMock()) as exit_mock:
            await self.engine._tick_supervisor()
            exit_mock.assert_awaited_once_with(self.engine._position, "TIME")

    async def test_legacy_saved_settings_migrate_without_losing_position(self):
        from dataclasses import asdict
        cfg = asdict(self.engine.cfg)
        del cfg["target_only"]
        algo_module.store.load_state.return_value = {
            "day": self.engine._day, "config": cfg, "position": asdict(self.engine._position),
            "enabled": True,
        }
        self.engine.cfg.target_only = True
        self.engine._restore()
        self.assertTrue(self.engine.cfg.target_only)
        self.assertTrue(self.engine._position.protective_policy_pending)
        self.assertEqual(self.engine._position.order_id, "entry-1")

    async def test_legacy_sl_migration_waits_for_confirmed_cancel(self):
        pos = self.engine._position
        pos.protective_policy_pending = True
        pos.protective_order_id = "old-sl"
        self.client.get_order_by_id.return_value = {"orderStatus": "PENDING", "orderId": "old-sl"}
        self.assertFalse(await self.engine._migrate_protective_policy(pos))
        self.assertTrue(pos.protective_policy_pending)
        self.client.place_order.assert_not_awaited()

    async def test_legacy_stop_is_replaced_by_90_percent_far_stop_after_cancel(self):
        pos = self.engine._position
        pos.protective_order_id = "old-sl"
        pos.protective_policy_pending = True
        self.engine.cfg.target_only = True
        self.engine.cfg.protective_sl = True
        self.client.get_order_by_id.side_effect = [
            {"orderStatus": "PENDING", "orderId": "old-sl"},
            {"orderStatus": "CANCELLED", "orderId": "old-sl"},
            {"orderStatus": "PENDING", "orderId": "far-sl"},
        ]
        with patch.object(algo_module.feed, "price", return_value=127), \
                patch.object(algo_module.feed, "age", return_value=0), \
                patch.object(self.engine, "_send_order", AsyncMock(return_value={
                    "ok": True, "orderId": "far-sl",
                })) as send:
            await self.engine.reconcile()
        self.client.cancel_order.assert_awaited_once_with("old-sl")
        send.assert_awaited_once()
        self.assertEqual(send.await_args.kwargs["trigger"], 12.8)
        self.assertEqual(pos.protective_order_id, "far-sl")
        self.assertFalse(pos.protective_policy_pending)
        self.assertIs(self.engine._position, pos)

    async def test_paper_far_stop_is_simulated_at_same_premium_threshold(self):
        self.engine.cfg.target_only = True
        self.engine.cfg.protective_sl = True
        self.engine._position = position(dry_run=True, entry_premium=100, premium=10)
        with patch.object(algo_module.feed, "age", return_value=0), \
                patch.object(self.engine, "_exit_position_locked", AsyncMock()) as exit_mock:
            await self.engine._manage_position()
            exit_mock.assert_awaited_once_with(self.engine._position, "PROTECTIVE_SL")

    async def test_target_only_risk_estimate_uses_90_percent_premium(self):
        self.engine.cfg.target_only = True
        self.engine.cfg.protective_sl = True
        leg = DayLeg("NIFTY", option_ltp=100, or_high=200, or_low=100)
        self.engine._balance = AsyncMock(return_value=100000)
        with patch.object(self.engine, "_delta_for", return_value=.5):
            sizing = await self.engine._size_position(leg)
        self.assertEqual(sizing["riskPerLot"], 5850)

    async def test_manual_far_sl_defaults_match_algo_for_long_option(self):
        from app.orders import OrderRequest, build_super_order
        payload = build_super_order(OrderRequest(
            index_key="NIFTY", security_id="47153", option_type="CE", transaction_type="BUY",
            side="", quantity=1, price=100, target_pct=5,
        ))
        self.assertEqual(payload["stopLossPrice"], 10)
        self.assertEqual(payload["targetPrice"], 105)

    async def test_minimum_lot_floor_never_exceeds_actual_cash(self):
        self.engine.cfg.min_lots = 5
        self.engine.cfg.max_lots = 10
        leg = DayLeg("NIFTY", option_ltp=100, or_high=200, or_low=100)
        self.engine._balance = AsyncMock(return_value=7000)
        with patch.object(self.engine, "_delta_for", return_value=.5):
            sizing = await self.engine._size_position(leg)
        self.assertLess(sizing["lots"], 5)
        self.assertLessEqual(sizing["lots"] * sizing["premiumPerLot"], 7000)

    async def test_smaller_broker_holding_preserves_entry_qty_and_resizes(self):
        self.client.get_positions.return_value[0]["netQty"] = 30
        with patch.object(self.engine, "_place_protective_sl", AsyncMock()) as protective:
            await self.engine.reconcile()
        self.assertEqual(self.engine._position.quantity, 30)
        self.assertEqual(self.engine._position.entry_quantity, 65)
        protective.assert_not_awaited()

    async def test_pending_entry_panic_cancels_buy_instead_of_selling_unfilled(self):
        pos = self.engine._position
        pos.execution_state = "entry_pending"
        self.client.get_order_by_id.side_effect = [
            {"orderStatus": "PENDING", "orderId": "entry-1"},
            {"orderStatus": "CANCELLED", "orderId": "entry-1", "filledQty": 0},
        ]
        await self.engine._exit_position(pos, "PANIC")
        self.client.cancel_order.assert_awaited_once_with("entry-1")
        self.client.place_order.assert_not_awaited()
        self.assertIsNone(self.engine._position)

    async def test_positions_exit_for_algo_uses_shared_execution(self):
        from app.routers import account
        body = account.SquareOffBody(securityId="47153", quantity=65, transactionType="SELL")
        with patch.object(account, "algo", self.engine), \
                patch.object(self.engine, "_exit_position_locked", AsyncMock()) as exit_mock:
            await account._square_off_locked(body)
        exit_mock.assert_awaited_once_with(self.engine._position, "MANUAL")
        self.client.place_order.assert_not_awaited()

    async def test_manual_exit_cannot_sell_a_closed_holding(self):
        from app.routers import account
        from fastapi import HTTPException
        self.engine._position = None
        self.client.get_positions.return_value = []
        self.client.get_order_book = AsyncMock(return_value=[])
        self.client.get_super_orders = AsyncMock(return_value=[])
        body = account.SquareOffBody(securityId="47153", quantity=65, transactionType="SELL")
        with patch.object(account, "algo", self.engine), \
                patch.object(account, "get_dhan_client", return_value=self.client), \
                patch.object(account.runtime, "is_dry_run", return_value=False), \
                patch.object(account, "_pending_exits", {}):
            with self.assertRaises(HTTPException):
                await account._square_off_locked(body)
        self.client.place_order.assert_not_awaited()

    async def test_manual_exit_waits_for_super_leg_cancellation(self):
        from app.routers import account
        from fastapi import HTTPException
        self.engine._position = None
        self.client.cancel_super_order = AsyncMock()
        self.client.get_super_orders = AsyncMock(return_value=[{
            "securityId": "47153", "exchangeSegment": "NSE_FNO", "productType": "INTRADAY",
            "orderStatus": "TRADED", "orderId": "super-1",
            "legDetails": [{"legName": "TARGET_LEG", "orderStatus": "PENDING"}],
        }])
        body = account.SquareOffBody(securityId="47153", quantity=65, transactionType="SELL")
        with patch.object(account, "algo", self.engine), \
                patch.object(account, "get_dhan_client", return_value=self.client), \
                patch.object(account.runtime, "is_dry_run", return_value=False), \
                patch.object(account, "_pending_exits", {}):
            with self.assertRaises(HTTPException):
                await account._square_off_locked(body)
        self.client.cancel_super_order.assert_awaited_once_with("super-1", "TARGET_LEG")
        self.client.place_order.assert_not_awaited()

    async def test_manual_entry_cannot_mix_into_algo_contract(self):
        from app.routers import orders
        from fastapi import HTTPException
        body = orders.OrderPlaceBody(
            indexKey="NIFTY", securityId="47153", optionType="CE",
            transactionType="BUY", quantity=1, price=128, targetPct=5, pin="",
        )
        with patch.object(orders, "algo", self.engine), patch.object(orders, "verify_order_pin"):
            with self.assertRaises(HTTPException) as caught:
                await orders._place_locked(body)
        self.assertEqual(caught.exception.status_code, 409)
        self.client.place_order.assert_not_awaited()

    async def test_super_stop_fill_during_cancel_blocks_second_sell(self):
        from app.routers import account
        from fastapi import HTTPException
        self.engine._position = None
        self.client.cancel_super_order = AsyncMock()
        base = dict(securityId="47153", exchangeSegment="NSE_FNO", productType="INTRADAY",
                    orderStatus="TRADED", orderId="super-1")
        self.client.get_super_orders = AsyncMock(side_effect=[
            [base | {"legDetails": [{"legName": "STOP_LOSS_LEG", "orderStatus": "PENDING"}]}],
            [base | {"legDetails": [{"legName": "STOP_LOSS_LEG", "orderStatus": "TRADED", "filledQty": 65}]}],
        ])
        body = account.SquareOffBody(securityId="47153", quantity=65, transactionType="SELL")
        with patch.object(account, "algo", self.engine), \
                patch.object(account, "get_dhan_client", return_value=self.client), \
                patch.object(account.runtime, "is_dry_run", return_value=False), \
                patch.object(account, "_pending_exits", {}):
            with self.assertRaises(HTTPException):
                await account._square_off_locked(body)
        self.client.place_order.assert_not_awaited()

    async def test_filled_entry_leg_does_not_block_manual_exit_after_exit_legs_cancel(self):
        from app.routers import account
        self.engine._position = None
        self.client.get_super_orders = AsyncMock(return_value=[{
            "securityId": "47153", "exchangeSegment": "NSE_FNO", "productType": "INTRADAY",
            "orderStatus": "TRADED", "orderId": "super-1", "legDetails": [
                {"legName": "ENTRY_LEG", "orderStatus": "TRADED", "filledQty": 65},
                {"legName": "TARGET_LEG", "orderStatus": "CANCELLED"},
                {"legName": "STOP_LOSS_LEG", "orderStatus": "CANCELLED"},
            ],
        }])
        body = account.SquareOffBody(securityId="47153", quantity=65, transactionType="SELL")
        with patch.object(account, "algo", self.engine), \
                patch.object(account, "get_dhan_client", return_value=self.client), \
                patch.object(account.runtime, "is_dry_run", return_value=False), \
                patch.object(account.audit, "log_order"), \
                patch.object(account, "_pending_exits", {}):
            await account._square_off_locked(body)
        self.client.place_order.assert_awaited_once()

    async def test_stop_cancel_request_not_replayed_while_pending(self):
        pos = self.engine._position
        pos.protective_order_id = "sl-1"
        self.client.get_order_by_id.return_value = {"orderStatus": "PENDING", "orderId": "sl-1"}
        await self.engine._cancel_protective(pos)
        await self.engine._cancel_protective(pos)
        self.client.cancel_order.assert_awaited_once_with("sl-1")


class FeedRegressionTests(unittest.TestCase):
    def test_same_security_id_on_two_exchanges_does_not_mix_prices(self):
        feed = MarketFeed()
        feed.subscribe(123, "NSE_FNO")
        feed.subscribe(123, "BSE_FNO")
        def packet(segment, price):
            return struct.pack("<BHBi f", 2, 16, segment, 123, price) + struct.pack("<i", 0)
        feed._handle_packet(packet(2, 100) + packet(8, 200))
        self.assertEqual(feed.price(123, "NSE_FNO"), 100)
        self.assertEqual(feed.price(123, "BSE_FNO"), 200)
        self.assertEqual(feed._grouped(), {"NSE_FNO": [123], "BSE_FNO": [123]})
        self.assertIsNone(feed.price(123))
        feed.unsubscribe(123, "NSE_FNO")
        self.assertEqual(feed.price(123, "BSE_FNO"), 200)
        self.assertIsNone(feed.price(123, "NSE_FNO"))

    def test_combined_packets_update_both_instruments(self):
        feed = MarketFeed()
        packet = lambda sid, price: struct.pack("<BHBi f", 2, 16, 0, sid, price) + struct.pack("<i", 0)
        feed._handle_packet(packet(13, 22750) + packet(51, 73000))
        self.assertEqual(feed.price(13), 22750)
        self.assertEqual(feed.price(51), 73000)
        self.assertIsNotNone(feed.age(13))

    def test_unchanged_price_still_notifies_and_refreshes_age(self):
        feed = MarketFeed()
        callback = MagicMock()
        feed.set_on_tick(callback)
        feed._apply(13, 100)
        feed._apply(13, 100)
        self.assertEqual(callback.call_count, 2)
        self.assertLess(feed.age(13), 1)

    def test_invalid_prices_never_enter_cache(self):
        feed = MarketFeed()
        for price in [float("nan"), float("inf"), 0, -1]:
            feed._apply(13, price)
        self.assertIsNone(feed.price(13))

    def test_silent_socket_not_reported_live(self):
        feed = MarketFeed()
        feed._connected = True
        feed._last_ws_msg_at = time.time() - 30
        self.assertFalse(feed.status()["webSocket"])


class RateAndCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_calls_obey_gap_and_rolling_budget(self):
        clock = [0.0]
        async def sleep(delay):
            clock[0] += delay
        gate = RateGate(.12, ((2, 60),))
        with patch("app.rate_limit.time.monotonic", side_effect=lambda: clock[0]), \
                patch("app.rate_limit.asyncio.sleep", side_effect=sleep):
            await gate.acquire()
            await gate.acquire()
            self.assertAlmostEqual(clock[0], .12)
            await gate.acquire()
            self.assertAlmostEqual(clock[0], 60)

    async def test_shared_cooldown_is_obeyed(self):
        clock = [0.0]
        async def sleep(delay):
            clock[0] += delay
        gate = RateGate(.1)
        with patch("app.rate_limit.time.monotonic", side_effect=lambda: clock[0]), \
                patch("app.rate_limit.asyncio.sleep", side_effect=sleep):
            gate.defer(3)
            await gate.acquire()
            self.assertEqual(clock[0], 3)

    async def test_minute_hour_and_day_quotas_are_all_enforced(self):
        clock = [0.0]
        async def sleep(delay):
            clock[0] += delay
        gate = RateGate(.12, ((2, 60), (3, 3600), (4, 86400)))
        with patch("app.rate_limit.time.monotonic", side_effect=lambda: clock[0]), \
                patch("app.rate_limit.asyncio.sleep", side_effect=sleep):
            stamps = []
            for _ in range(5):
                await gate.acquire()
                stamps.append(clock[0])
        self.assertEqual(stamps, [0, .12, 60, 3600, 86400])

    async def test_categories_share_expected_budget(self):
        self.assertIs(gate_for("POST", "/charts/intraday"), DATA_GATE)
        self.assertIs(gate_for("POST", "/charts/historical"), DATA_GATE)
        self.assertIs(gate_for("POST", "/marketfeed/ltp"), QUOTE_GATE)
        self.assertIs(gate_for("POST", "/orders"), ORDER_GATE)
        self.assertIs(gate_for("DELETE", "/super/orders/1/STOP_LOSS_LEG"), ORDER_GATE)

    async def test_concurrent_position_readers_share_one_request(self):
        client = DhanClient()
        client._request = AsyncMock(return_value=[{
            "securityId": "1", "netQty": 65, "exchangeSegment": "NSE_FNO", "productType": "INTRADAY",
        }])
        try:
            results = await asyncio.gather(*(client.get_positions() for _ in range(10)))
            client._request.assert_awaited_once()
            results[0][0]["netQty"] = 0
            self.assertEqual(results[1][0]["netQty"], 65)
            await client.get_positions(fresh=True)
            self.assertEqual(client._request.await_count, 2)
        finally:
            await client.aclose()

    async def test_bulk_throttle_false_still_uses_shared_data_gate(self):
        import httpx
        client = DhanClient()
        await client._client.aclose()
        client._mock = False
        client._client = httpx.AsyncClient(
            base_url="https://api.dhan.co/v2",
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"high": []})),
        )
        gate = MagicMock(acquire=AsyncMock())
        try:
            with patch("app.dhan_client.gate_for", return_value=gate) as pick_gate:
                await client._request("POST", "/charts/intraday", throttle=False)
                pick_gate.assert_called_once_with("POST", "/charts/intraday", client._client_id)
                gate.acquire.assert_awaited_once()
        finally:
            await client.aclose()

    async def test_429_sets_shared_cooldown_without_replaying_order(self):
        import httpx
        client = DhanClient()
        await client._client.aclose()
        client._mock = False
        handler = MagicMock(return_value=httpx.Response(
            429, headers={"Retry-After": "7"},
            json={"errorCode": "DH-904", "errorMessage": "rate limited"},
        ))
        client._client = httpx.AsyncClient(base_url="https://api.dhan.co/v2", transport=httpx.MockTransport(handler))
        gate = MagicMock(acquire=AsyncMock())
        try:
            with patch("app.dhan_client.gate_for", return_value=gate):
                with self.assertRaises(DhanAPIError):
                    await client.place_order({"securityId": "test"})
            handler.assert_called_once()
            gate.defer.assert_called_once_with(7)
        finally:
            await client.aclose()


class ManualOrderAuditTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from app import audit
        from app.routers import orders
        self.audit = audit
        self.orders = orders
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        db_patch = patch.object(audit, "DB_PATH", Path(self.tmp.name) / "audit.sqlite3")
        db_patch.start()
        self.addCleanup(db_patch.stop)
        audit.init_db()
        guard = patch("httpx.AsyncClient.request", side_effect=AssertionError("No network in tests"))
        guard.start()
        self.addCleanup(guard.stop)
        self.client = MagicMock(
            get_fund_limit=AsyncMock(return_value={"availabelBalance": 100000}),
            place_super_order=AsyncMock(return_value={"orderId": "manual-1", "orderStatus": "PENDING"}),
        )
        for obj, name, value in (
            (orders, "get_dhan_client", self.client),
            (orders, "algo", MagicMock(_position=None)),
        ):
            p = patch.object(obj, name, return_value=value) if name == "get_dhan_client" else patch.object(obj, name, value)
            p.start()
            self.addCleanup(p.stop)
        pin_patch = patch.object(orders, "verify_order_pin")
        pin_patch.start()
        self.addCleanup(pin_patch.stop)
        mode_patch = patch.object(orders.runtime, "is_dry_run", return_value=False)
        mode_patch.start()
        self.addCleanup(mode_patch.stop)
        self.body = orders.OrderPlaceBody(
            indexKey="NIFTY", securityId="47153", optionType="CE",
            transactionType="BUY", quantity=1, price=100, targetPct=5, pin="",
        )

    async def test_manual_retry_reuses_receipt_and_never_repeats_buy(self):
        first = await self.orders._place_locked(self.body)
        self.client.get_fund_limit.side_effect = AssertionError("Receipt lookup must not need fresh funds")
        again = await self.orders._place_locked(self.body)
        self.assertEqual(first, again)
        self.client.place_super_order.assert_awaited_once()
        payload = self.client.place_super_order.call_args.args[0]
        self.assertLessEqual(len(payload["correlationId"]), 30)
        self.assertEqual(payload["quantity"], 65)

    async def test_manual_request_persistence_failure_prevents_broker_write(self):
        import sqlite3
        from fastapi import HTTPException
        with patch.object(self.audit, "claim_manual_request", side_effect=sqlite3.OperationalError("disk full")):
            with self.assertRaises(HTTPException) as caught:
                await self.orders._place_locked(self.body)
        self.assertEqual(caught.exception.status_code, 503)
        self.client.place_super_order.assert_not_awaited()

    async def test_manual_timeout_is_persisted_and_not_replayed(self):
        from fastapi import HTTPException
        self.client.place_super_order.side_effect = DhanAPIError(0, "NETWORK", "Timed out")
        for expected in (502, 409):
            with self.assertRaises(HTTPException) as caught:
                await self.orders._place_locked(self.body)
            self.assertEqual(caught.exception.status_code, expected)
        self.client.place_super_order.assert_awaited_once()

    async def test_reusing_request_id_with_changed_inputs_is_blocked(self):
        from fastapi import HTTPException
        await self.orders._place_locked(self.body)
        self.body.quantity = 2
        with self.assertRaises(HTTPException) as caught:
            await self.orders._place_locked(self.body)
        self.assertEqual(caught.exception.status_code, 409)
        self.client.place_super_order.assert_awaited_once()

    async def test_unavailable_or_malformed_funds_never_approve_entry(self):
        from fastapi import HTTPException
        for funds in ({"sodLimit": 100000}, {"availabelBalance": "bad"},
                      {"availabelBalance": float("nan")}):
            self.client.get_fund_limit.return_value = funds
            with self.assertRaises(HTTPException) as caught:
                await self.orders._place_locked(self.body)
            self.assertEqual(caught.exception.status_code, 503)
        self.client.get_fund_limit.side_effect = DhanAPIError(401, "TOKEN", "Expired")
        with self.assertRaises(HTTPException):
            await self.orders._place_locked(self.body)
        self.client.place_super_order.assert_not_awaited()

    async def test_market_reference_is_refreshed_from_exact_contract(self):
        self.body.orderType = "MARKET"
        snap = {"stale": False, "ageSeconds": 1, "rows": [
            {"ce": {"securityId": 999, "ltp": 1000}},
            {"ce": {"securityId": 47153, "ltp": 120}},
        ]}
        with patch.object(self.orders.chain_engine, "snapshot", return_value=snap):
            result = await self.orders._place_locked(self.body)
        self.assertEqual(result["preview"]["entryPrice"], 120)
        payload = self.client.place_super_order.call_args.args[0]
        self.assertEqual(payload["targetPrice"], 126)
        self.assertEqual(payload["stopLossPrice"], 12)

    async def test_stale_or_wrong_contract_market_quote_blocks_order(self):
        from fastapi import HTTPException
        self.body.orderType = "MARKET"
        for snap in (None, {"stale": True}, {"ageSeconds": 13},
                     {"ageSeconds": 1, "rows": [{"ce": {"securityId": 999, "ltp": 120}}]}):
            with patch.object(self.orders.chain_engine, "snapshot", return_value=snap):
                with self.assertRaises(HTTPException):
                    await self.orders._place_locked(self.body)
        self.client.place_super_order.assert_not_awaited()

    async def test_manual_exit_identity_survives_memory_reset(self):
        key = ("47153", "NSE_FNO", "INTRADAY")
        self.audit.save_manual_exit(key, "MAN-X-test")
        self.assertEqual(self.audit.pending_manual_exits(), {key: "MAN-X-test"})
        self.audit.save_manual_exit(key, None)
        self.assertEqual(self.audit.pending_manual_exits(), {})

    async def test_invalid_tick_lot_and_rounded_prices_are_rejected(self):
        from app.orders import build_super_order
        from fastapi import HTTPException
        req = self.orders._to_order_request(self.body)
        for changes in ({"price": 100.01}, {"price": float("inf")},
                        {"price": .05}, {"target_pct": .1, "price": 1},
                        {"transaction_type": "SELL", "target_pct": 100},
                        {"quantity": 1, "lots": False},
                        {"security_id": ""}, {"stop_loss_pct": float("nan")}):
            from dataclasses import replace
            with self.subTest(changes=changes), self.assertRaises(HTTPException):
                build_super_order(replace(req, **changes))

    async def test_manual_quantity_uses_same_lot_override_as_algo(self):
        from app.orders import build_super_order, preview
        req = self.orders._to_order_request(self.body)
        with patch("app.config.get_settings", return_value=MagicMock(algo_lot_override={"NIFTY": 75})):
            self.assertEqual(build_super_order(req)["quantity"], 75)
            self.assertEqual(preview(req)["lotSize"], 75)


class PortfolioQuoteTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_manual_exit_does_not_hide_real_broker_flat_portfolio(self):
        from app.routers import account as module
        key = ("47153", "NSE_FNO", "INTRADAY")
        pending = {key: "old-request"}
        client = MagicMock(
            get_positions=AsyncMock(return_value=[]),
            get_order_by_correlation=AsyncMock(side_effect=DhanAPIError(0, "UNKNOWN_ORDER", "Old order unavailable")),
        )
        with patch.object(module.audit, "pending_manual_exits", return_value=pending), \
                patch.object(module.audit, "save_manual_exit") as save, \
                patch.object(module, "get_dhan_client", return_value=client), \
                patch.object(module, "_pending_exits", {}):
            self.assertEqual(await module.positions("session"), [])
        self.assertEqual(pending[key], "old-request")
        save.assert_not_called()

    async def test_rest_recovers_stale_option_even_when_index_keeps_websocket_healthy(self):
        from app import feed as module
        f = MarketFeed()
        f.subscribe(13, "IDX_I")
        f.subscribe(47153, "NSE_FNO")
        f._apply(13, 23000, "IDX_I")
        f._connected = True
        f._last_ws_msg_at = time.time()
        f._running = True
        async def quotes(payload):
            f._running = False
            return {"data": {"NSE_FNO": {"47153": {"last_price": 100}}}}
        client = MagicMock(get_ltp=AsyncMock(side_effect=quotes))
        with patch.object(module, "get_dhan_client", return_value=client), \
                patch.object(module.asyncio, "sleep", AsyncMock()):
            await f._rest_loop()
        client.get_ltp.assert_awaited_once_with({"NSE_FNO": [47153]})
        self.assertEqual(f.price(47153, "NSE_FNO"), 100)

    async def test_segment_aware_quotes_keep_broker_quantities_and_pnl(self):
        from app import feed as module
        rows = [
            {"securityId": "123", "exchangeSegment": "NSE_FNO", "netQty": 65, "unrealizedProfit": 25},
            {"securityId": "123", "exchangeSegment": "BSE_FNO", "netQty": -20, "unrealizedProfit": 35},
        ]
        client = MagicMock(get_ltp=AsyncMock(return_value={"data": {
            "NSE_FNO": {"123": {"last_price": 100}},
            "BSE_FNO": {"123": {"last_price": 200}},
        }}))
        with patch.object(module, "get_dhan_client", return_value=client):
            result = await module.attach_ltp(rows)
        self.assertEqual([r["ltp"] for r in result], [100, 200])
        self.assertEqual([r["netQty"] for r in result], [65, -20])
        self.assertEqual([r["unrealizedProfit"] for r in result], [25, 35])

    async def test_quote_failure_keeps_holdings_visible_and_marks_price_stale(self):
        from app import feed as module
        rows = [{"securityId": "123", "exchangeSegment": "NSE_FNO", "netQty": 65, "ltp": 200}]
        client = MagicMock(get_ltp=AsyncMock(side_effect=DhanAPIError(429, "DH-904", "Rate limited")))
        with patch.object(module, "get_dhan_client", return_value=client):
            result = await module.attach_ltp(rows)
        self.assertEqual(result[0]["netQty"], 65)
        self.assertIsNone(result[0]["ltp"])
        self.assertEqual(result[0]["quoteError"], "Rate limited")


class ExpirySyncTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from app import optionchain
        self.module = optionchain
        self.engine = optionchain.OptionChainEngine()
        self.client = MagicMock(
            get_expiry_list=AsyncMock(return_value=["2026-10-19", "2026-10-26"]),
            get_option_chain=AsyncMock(return_value={"last_price": 23000, "oc": {}}),
        )

    async def test_expiry_change_invalidates_old_contract_snapshot(self):
        self.engine._expiries["NIFTY"] = ["2026-10-19", "2026-10-26"]
        self.engine._snapshots["NIFTY"] = self.module.ChainSnapshot("NIFTY", "2026-10-19", 23000, [])
        self.engine.set_expiry("NIFTY", "2026-10-26")
        self.assertIsNone(self.engine.snapshot("NIFTY"))
        with self.assertRaises(ValueError):
            self.engine.set_expiry("NIFTY", "expired")

    async def test_new_day_refreshes_expiry_list_and_drops_expired_override(self):
        self.engine._expiry_day["NIFTY"] = "2000-01-01"
        self.engine._expiries["NIFTY"] = ["2000-01-01"]
        self.engine._expiry_override["NIFTY"] = "2000-01-01"
        with patch.object(self.module, "get_dhan_client", return_value=self.client):
            await self.engine._fetch_one("NIFTY")
        self.client.get_expiry_list.assert_awaited_once()
        self.assertEqual(self.engine.snapshot("NIFTY")["expiry"], "2026-10-19")
        self.assertNotIn("NIFTY", self.engine._expiry_override)

    async def test_slow_previous_expiry_response_does_not_overwrite_new_selection(self):
        async def change_while_fetching(*args):
            self.engine.set_expiry("NIFTY", "2026-10-26")
            return {"last_price": 23000, "oc": {}}
        self.client.get_option_chain.side_effect = change_while_fetching
        with patch.object(self.module, "get_dhan_client", return_value=self.client):
            await self.engine._fetch_one("NIFTY")
        self.assertIsNone(self.engine.snapshot("NIFTY"))


if __name__ == "__main__":
    unittest.main()
