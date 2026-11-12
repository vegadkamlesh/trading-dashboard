import asyncio
import json
import os
import tempfile
import time
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException

import app.algo as algo_module
from app.algo import DayLeg
from app.algo2 import calculations as calc
from app.algo2 import daily_cache
from app.algo2 import engine as engine_module
from app.algo2.engine import Algo2Group, Algo2Position, parse_windows, validate_algo2_config
from app.algo2.market_data import Series, parse_candles, resolve_futures_from_csv
from app.algo2.news_events import NewsEventFetcher
from app.dhan_client import DhanAPIError


def chain_row(strike, ce_oi, pe_oi, ce_delta, pe_delta, ce_gamma=0.001, pe_gamma=0.001):
    return {
        "strike": strike,
        "ce": {"securityId": int(strike) + 1, "ltp": 100.0, "oi": ce_oi,
               "greeks": {"delta": ce_delta, "gamma": ce_gamma}},
        "pe": {"securityId": int(strike) + 2, "ltp": 90.0, "oi": pe_oi,
               "greeks": {"delta": pe_delta, "gamma": pe_gamma}},
    }


CHAIN = [
    chain_row(23400, 1000, 5000, 0.80, -0.20),
    chain_row(23450, 2000, 4000, 0.70, -0.30),
    chain_row(23500, 3000, 3000, 0.50, -0.50, 0.004, 0.002),
    chain_row(23550, 6000, 1500, 0.30, -0.70),
    chain_row(23600, 9000, 500, 0.15, -0.85),
]


class NewsFreezeTests(unittest.TestCase):
    def test_freeze_window_is_15_minutes_each_side(self):
        with tempfile.TemporaryDirectory() as tmp:
            news = NewsEventFetcher(tmp, window_minutes=15)
            news.add({"date": "2026-12-16", "time": "10:00", "name": "RBI policy"})
            day = "2026-12-16"
            self.assertFalse(news.is_news_freeze_active(datetime.fromisoformat(f"{day}T09:44:59"))[0])
            self.assertEqual(news.is_news_freeze_active(datetime.fromisoformat(f"{day}T09:45:00")),
                             (True, "RBI policy"))
            self.assertTrue(news.is_news_freeze_active(datetime.fromisoformat(f"{day}T10:15:00"))[0])
            self.assertFalse(news.is_news_freeze_active(datetime.fromisoformat(f"{day}T10:15:01"))[0])
            self.assertFalse(news.is_news_freeze_active(datetime.fromisoformat("2026-12-17T10:00:00"))[0])

    def test_untimed_event_blocks_only_when_enabled_and_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            news = NewsEventFetcher(tmp)
            ev = news.add({"date": "2026-12-10", "name": "TCS results"})
            now = datetime.fromisoformat("2026-12-10T11:00:00")
            self.assertFalse(news.is_news_freeze_active(now)[0])
            news.block_untimed = True
            self.assertTrue(news.is_news_freeze_active(now)[0])
            reloaded = NewsEventFetcher(tmp)
            self.assertIn(ev["id"], [e["id"] for e in reloaded.events()])
            self.assertTrue(reloaded.remove(ev["id"]))
            with self.assertRaises(ValueError):
                news.add({"date": "bad", "name": "x"})

    def test_seed_contains_verified_rbi_dates(self):
        with tempfile.TemporaryDirectory() as tmp:
            dates = {e["date"] for e in NewsEventFetcher(tmp).events()}
            self.assertIn("2026-12-04", dates)
            self.assertIn("2027-02-05", dates)


class CalculationTests(unittest.TestCase):
    def test_net_delta_gex_pcr(self):
        expected_delta = sum(r["pe"]["oi"] * abs(r["pe"]["greeks"]["delta"]) - r["ce"]["oi"] * r["ce"]["greeks"]["delta"]
                             for r in CHAIN)
        self.assertAlmostEqual(calc.compute_net_delta(CHAIN), expected_delta)
        expected_gex = sum(r["pe"]["oi"] * r["pe"]["greeks"]["gamma"] - r["ce"]["oi"] * r["ce"]["greeks"]["gamma"]
                           for r in CHAIN)
        self.assertAlmostEqual(calc.compute_net_gex(CHAIN), expected_gex)
        self.assertLess(calc.compute_net_gex(CHAIN), 0)
        self.assertAlmostEqual(calc.compute_pcr(CHAIN), 14000 / 21000)

    def test_oi_velocity_over_5_minutes(self):
        vel = calc.compute_oi_velocity({"ce": 9000, "pe": 13000}, {"ce": 10000, "pe": 10000})
        self.assertAlmostEqual(vel["ce"], -10.0)
        self.assertAlmostEqual(vel["pe"], 30.0)
        self.assertFalse(calc.oi_velocity_spike(vel, "pe", 30.0))
        self.assertTrue(calc.oi_velocity_spike(vel, "pe", 29.9))
        self.assertIsNone(calc.compute_oi_velocity({"ce": 1, "pe": 1}, {"ce": 0, "pe": 0})["ce"])

    def test_near_rows_wall_and_delta_strike(self):
        self.assertEqual([r["strike"] for r in calc.near_rows(CHAIN, 23510, 1)], [23450, 23500, 23550])
        self.assertEqual(calc.oi_wall(CHAIN, 23510, "CALL", 10), 23600)
        self.assertEqual(calc.oi_wall(CHAIN, 23510, "PUT", 10), 23400)
        self.assertEqual(calc.pick_delta_strike(CHAIN, 23510, "CALL", 0.70)["strike"], 23450)
        self.assertEqual(calc.pick_delta_strike(CHAIN, 23510, "PUT", 0.70)["strike"], 23550)

    def test_hsmi_vwap_profile_basis(self):
        self.assertAlmostEqual(calc.compute_hsmi({"A": 1.0, "B": -1.0, "C": None}, {"A": 3, "B": 1, "C": 5}), 0.5)
        candles = [{"high": 101, "low": 99, "close": 100, "volume": 100},
                   {"high": 103, "low": 101, "close": 102, "volume": 300}]
        bands = calc.compute_vwap_bands(candles, 1.5)
        self.assertAlmostEqual(bands["vwap"], 101.5)
        self.assertAlmostEqual(bands["upper"], 101.5 + 1.5 * bands["sd"])
        profile = calc.compute_market_profile(candles, 1.0)
        self.assertTrue(profile["val"] <= profile["poc"] <= profile["vah"])
        self.assertEqual(calc.compute_basis(100, 98), -2)
        self.assertTrue(calc.basis_divergence(True, -1))


class MarketDataTests(unittest.TestCase):
    def test_series_ago_and_candles(self):
        s = Series()
        s.add(100, at=1000)
        s.add(110, at=1300)
        self.assertEqual(s.ago(300, now=1300), 100)
        self.assertIsNone(s.ago(600, now=1300))
        rows = parse_candles({"timestamp": [2, 1], "open": [1, 1], "high": [3, 2], "low": [1, 1],
                              "close": [2, 2], "volume": [5, 6]})
        self.assertEqual([c["t"] for c in rows], [1.0, 2.0])

    def test_nearest_futures_from_scrip_master(self):
        csv_text = ("SEM_EXM_EXCH_ID,SEM_SMST_SECURITY_ID,SEM_INSTRUMENT_NAME,SEM_TRADING_SYMBOL,SEM_EXPIRY_DATE\n"
                    "NSE,58875,FUTIDX,NIFTY-Dec2026-FUT,2026-12-29 14:30:00\n"
                    "NSE,61471,FUTIDX,NIFTY-Nov2026-FUT,2026-11-23 14:30:00\n"
                    "NSE,11,FUTIDX,NIFTY-Oct2026-FUT,2026-10-27 14:30:00\n"
                    "BSE,1100929,FUTIDX,SENSEX-Nov2026-FUT,2026-11-26 14:30:00\n")
        out = resolve_futures_from_csv(csv_text, datetime(2026, 11, 10).date())
        self.assertEqual(out["NIFTY"]["securityId"], 61471)
        self.assertEqual(out["SENSEX"]["segment"], "BSE_FNO")


class ConfigTests(unittest.TestCase):
    def test_windows_and_validation(self):
        self.assertEqual(parse_windows("09:45-11:15,13:30-15:00"), [(585, 675), (810, 900)])
        with self.assertRaises(ValueError):
            parse_windows("11:00-10:00")
        cfg = engine_module.asdict(engine_module.Algo2Config())
        validate_algo2_config(cfg)
        with self.assertRaises(HTTPException):
            validate_algo2_config({**cfg, "nifty_weights": {"UNKNOWN": 1.0}})
        with self.assertRaises(HTTPException):
            validate_algo2_config({**cfg, "pcr_chop_low": 1.2})


class Algo2EngineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.client = MagicMock()
        self.client.place_order = AsyncMock(side_effect=AssertionError("paper mode must not place orders"))
        self.client.get_positions = AsyncMock(return_value=[])
        for target in (patch.object(algo_module, "get_dhan_client", return_value=self.client),
                       patch("httpx.AsyncClient.request", side_effect=AssertionError("no network in tests")),
                       patch.object(engine_module, "feed", MagicMock(age=MagicMock(return_value=0.2)))):
            target.start()
            self.addCleanup(target.stop)
        self.group = Algo2Group(self.tmp.name)
        self.assertFalse(self.group.dry_run)   # Algo 2 defaults to LIVE
        self.assertTrue(self.group.saved_enabled)   # ... and ON
        self.assertTrue(self.group.cfg.priority_over_algo1)
        self.group.dry_run = True              # tests below must never reach the broker
        self.engine = self.group.items["master"]
        self.engine.cfg.protective_sl = False
        self.engine._day = algo_module.now_ist().date().isoformat()
        self.engine._legs = {"NIFTY": DayLeg("NIFTY", status="active")}

    def position(self, **changes):
        values = dict(index_key="NIFTY", option_type="CALL", security_id=1, strike=23450, expiry="2026-11-17",
                      lots=3, quantity=195, entry_spot=23500.0, entry_premium=100.0, entry_time="10:00:00",
                      entry_ts=time.time(), or_range=20.0, target_spot=23540.0, stop_spot=23480.0,
                      initial_stop_spot=23480.0, dry_run=True, spot=23500.0, premium=100.0,
                      t1_spot=23540.0, entry_confirmed_ts=time.time())
        return Algo2Position(**(values | changes))

    async def test_paper_t1_partial_then_cost_stop_never_calls_broker(self):
        self.engine.cfg.partial_booking = True
        pos = self.position()
        self.engine._position = pos
        pos.spot, pos.premium = 23541.0, 130.0
        await self.engine._manage_position()
        self.assertTrue(pos.t1_done)
        self.assertEqual(pos.t1_qty, 130)      # 70% of 3 lots -> 2 lots of 65
        self.assertEqual(pos.quantity, 65)
        self.assertEqual(pos.stop_spot, 23500.0)
        self.assertEqual(pos.target_spot, 23560.0)  # max(2R, T1 + R) beyond entry
        pos.spot, pos.premium = 23499.0, 98.0
        await self.engine._manage_position()
        self.assertIsNone(self.engine._position)
        trade = self.engine._today_trades[-1]
        self.assertEqual(trade["exitReason"], "COST_STOP")
        self.assertEqual(trade["quantity"], 195)
        self.assertAlmostEqual(trade["premiumPnl"], 30.0 * 130 - 2.0 * 65)
        self.client.place_order.assert_not_awaited()

    async def test_single_lot_exits_fully_at_t1(self):
        self.engine._position = pos = self.position(lots=1, quantity=65)
        pos.spot, pos.premium = 23541.0, 120.0
        await self.engine._manage_position()
        self.assertIsNone(self.engine._position)
        self.assertEqual(self.engine._today_trades[-1]["exitReason"], "T1_FULL")

    async def test_default_two_r_target_exits_all_lots(self):
        self.engine._position = pos = self.position()
        pos.spot, pos.premium = 23541.0, 130.0
        await self.engine._manage_position()
        self.assertIsNone(self.engine._position)
        trade = self.engine._today_trades[-1]
        self.assertEqual((trade["exitReason"], trade["quantity"]), ("T1_FULL", 195))

    async def test_software_spot_stop_remains_active_without_broker_sl(self):
        self.assertFalse(self.engine.cfg.protective_sl)
        pos = self.position(spot=23479.0, premium=95.0)
        self.engine._position = pos
        with patch.object(self.engine, "_send_order", new_callable=AsyncMock,
                          return_value={"ok": True, "dryRun": True, "orderId": "DRY-stop"}) as send:
            await self.engine._manage_position()
        send.assert_awaited_once()
        self.assertEqual(send.await_args.kwargs["side"], "SELL")
        self.assertEqual(send.await_args.kwargs["order_type"], "MARKET")
        self.assertEqual(self.engine._today_trades[-1]["exitReason"], "STOP")

    async def test_entry_does_not_place_broker_protective_sl(self):
        leg = DayLeg("NIFTY", status="watching", or_high=23500.0, or_low=23480.0,
                     strike=23450.0, security_id=1, option_ltp=100.0, spot=23510.0,
                     expiry="2026-11-17", prepared_side="CALL")
        self.engine._legs = {"NIFTY": leg}
        sizing = {"lots": 1, "quantity": 65, "balance": 100000.0, "reason": "test"}
        with patch.object(self.engine, "_prepare_contract", new_callable=AsyncMock), \
                patch.object(self.engine, "_size_position", new_callable=AsyncMock, return_value=sizing), \
                patch.object(algo_module.feed, "price", return_value=100.0), \
                patch.object(algo_module.feed, "age", return_value=0.2), \
                patch.object(self.engine, "_send_order", new_callable=AsyncMock,
                             return_value={"ok": True, "dryRun": True, "orderId": "DRY-entry"}), \
                patch.object(self.engine, "_place_protective_sl", new_callable=AsyncMock) as protective_sl:
            await self.engine._enter(leg, "CALL")
        protective_sl.assert_not_awaited()
        self.assertIsNotNone(self.engine._position, leg.note or f"entry status: {leg.status}")
        self.assertEqual(self.engine._position.execution_state, "open")

    async def test_hold_rule_and_data_sl_are_off_by_default(self):
        self.engine._position = pos = self.position(entry_confirmed_ts=time.time() - 60 * 60, spot=23510.0)
        with patch.object(self.group.data, "oi_velocity", return_value={"ce": 25.0, "pe": 5.0}):
            await self.engine._manage_position()
        self.assertIs(self.engine._position, pos)
        self.assertFalse(any("minute rule" in line for line in self.engine._next_actions()))

    async def test_twenty_minute_rule_exits_when_t1_not_hit(self):
        self.engine.cfg.max_hold_minutes = 20
        self.engine._position = self.position(entry_confirmed_ts=time.time() - 20 * 60 - 1, spot=23510.0)
        await self.engine._manage_position()
        self.assertEqual(self.engine._today_trades[-1]["exitReason"], "STAGNANT_20M")

    async def test_data_sl_exits_on_call_oi_trap(self):
        self.engine.cfg.data_sl = True
        self.engine._position = self.position(spot=23510.0)
        with patch.object(self.group.data, "oi_velocity", return_value={"ce": 25.0, "pe": 5.0}):
            await self.engine._manage_position()
        self.assertEqual(self.engine._today_trades[-1]["exitReason"], "DATA_SL")

    async def test_put_stop_is_mirrored(self):
        self.engine._position = self.position(option_type="PUT", target_spot=23460.0, stop_spot=23520.0,
                                              initial_stop_spot=23520.0, t1_spot=23460.0)
        self.engine._position.spot = 23521.0
        await self.engine._manage_position()
        self.assertEqual(self.engine._today_trades[-1]["exitReason"], "STOP")

    async def test_levels_cap_stop_by_capital_and_use_oi_wall(self):
        self.engine.cfg.capital_sl_cap = True
        self.engine.cfg.oi_wall_target = True
        self.group.signal_ctx["NIFTY"] = {"candleLow": 23450.0, "candleHigh": 23510.0, "wall": 23700.0}
        leg = DayLeg("NIFTY", strike=23450)
        with patch.object(self.engine, "_delta_for", return_value=0.7):
            target, stop = self.engine._levels(leg, "CALL", 23500.0, 20.0, {"quantity": 65, "balance": 100000})
        cap = 100000 * 0.0075 / (0.7 * 65)
        self.assertAlmostEqual(stop, round(23500 - cap, 2))
        self.assertEqual(target, 23700.0)
        self.group.signal_ctx["NIFTY"]["wall"] = 23505.0      # too close -> 1:2 R:R
        with patch.object(self.engine, "_delta_for", return_value=0.7):
            target, _ = self.engine._levels(leg, "CALL", 23500.0, 20.0, {"quantity": 65, "balance": 100000})
        self.assertAlmostEqual(target, round(23500 + 2 * cap, 2))

    async def test_default_stop_is_breakout_candle_without_capital_cap(self):
        self.assertFalse(self.engine.cfg.capital_sl_cap)
        self.group.signal_ctx["NIFTY"] = {"candleLow": 23470.0, "candleHigh": 23510.0, "wall": None}
        with patch.object(self.engine, "_delta_for", return_value=0.7):
            target, stop = self.engine._levels(DayLeg("NIFTY"), "CALL", 23500.0, 20.0,
                                               {"quantity": 390, "balance": 100000})
        self.assertEqual(stop, 23470.0)
        self.assertEqual(target, 23560.0)
        self.group.signal_ctx["NIFTY"] = {"candleLow": 23470.0, "candleHigh": 23530.0, "wall": None}
        with patch.object(self.engine, "_delta_for", return_value=0.7):
            target, stop = self.engine._levels(DayLeg("NIFTY"), "PUT", 23500.0, 20.0,
                                               {"quantity": 390, "balance": 100000})
        self.assertEqual((target, stop), (23440.0, 23530.0))

    async def test_default_target_ignores_oi_wall(self):
        self.group.signal_ctx["NIFTY"] = {"candleLow": 23470.0, "candleHigh": 23510.0, "wall": 23700.0}
        with patch.object(self.engine, "_delta_for", return_value=0.7):
            target, stop = self.engine._levels(DayLeg("NIFTY"), "CALL", 23500.0, 20.0,
                                               {"quantity": 65, "balance": 100000})
        self.assertEqual((target, stop), (23560.0, 23470.0))

    async def test_stop_loss_exit_ends_entries_for_the_day(self):
        today = algo_module.now_ist().date().isoformat()
        self.assertIsNone(self.group.sl_hit_today(today))
        self.engine.cfg.stop_after_sl = True
        self.engine._today_trades = [{"date": today, "index": "NIFTY", "direction": "CALL",
                                      "exitReason": "COST_STOP", "exitTime": "10:05:00"}]
        self.assertIsNone(self.group.sl_hit_today(today))
        self.engine._today_trades.append({"date": today, "index": "NIFTY", "direction": "CALL",
                                          "exitReason": "STOP", "exitTime": "10:30:00"})
        self.assertIn("STOP", self.group.sl_hit_today(today))
        self.engine.cfg.stop_after_sl = False
        self.assertIsNone(self.group.sl_hit_today(today))

    async def test_five_minute_report_once_per_slot_and_phase_text(self):
        self.engine.enabled = True
        self.group.metrics["NIFTY"] = {
            "spot": 23500.0, "triggerUp": 23512.5, "triggerDown": 23480.0,
            "checks": {"CALL": [{"key": "news", "gate": True, "enabled": True, "pass": True},
                                {"key": "gex_negative", "enabled": True, "pass": False}],
                       "PUT": [{"key": "news", "gate": True, "enabled": True, "pass": True}]}}
        logs = []
        with patch.object(self.engine, "_log", side_effect=lambda msg, **kw: logs.append(msg)), \
                patch.object(self.group, "flat_phase", return_value="waiting_window"):
            now = datetime(2026, 11, 11, 11, 20, 3)
            self.group._notify_updates(now)
            self.group._notify_updates(now.replace(second=40))
            self.group._notify_updates(datetime(2026, 11, 11, 11, 21, 0))
        reports = [m for m in logs if m.startswith("5-min status")]
        self.assertEqual(len(reports), 1)
        self.assertIn("CALL above 23,512.5", reports[0])
        self.assertIn("CALL gates OK, score 0/1 (need 1)", reports[0])
        self.assertIn("PUT gates OK, score 0/0 (need 0) - READY", reports[0])
        self.assertEqual(len(self.group.reports), 1)
        self.assertEqual(len(logs), 1)          # nothing else logged between reports
        day = f"{self.tmp.name}/history/2026-11-11/reports.jsonl"
        with open(day, encoding="utf-8") as fh:
            saved = [__import__("json").loads(line) for line in fh]
        self.assertEqual(len(saved), 1)
        self.assertIn("5-min status", saved[0]["text"])
        self.assertIn("NIFTY", saved[0]["indices"])

    async def test_blocked_breakout_goes_into_next_report_not_instant(self):
        self.engine.enabled = True
        m = {"spot": 23520.0, "triggerUp": 23512.5}
        logs = []
        with patch.object(self.engine, "_log", side_effect=lambda msg, **kw: logs.append(msg)), \
                patch.object(self.group, "flat_phase", return_value="scanning"):
            self.group._notify_updates(datetime(2026, 11, 11, 11, 21, 0))
            snap = list(self.group.snapshot["lines"])
            self.group._note_blocked("NIFTY", "CALL", 1.0, ["gex_negative"], m)
            self.group._note_blocked("NIFTY", "CALL", 1.0, ["gex_negative"], m)
            self.group._notify_updates(datetime(2026, 11, 11, 11, 23, 0))
            self.assertEqual(logs, [])
            self.assertEqual(self.group.snapshot["lines"], snap)   # unchanged until the 5-min slot
            self.group._notify_updates(datetime(2026, 11, 11, 11, 25, 0))
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].count("NIFTY CALL @ 23,520.0 (fail: Net GEX negative)"), 1)
        self.assertEqual(self.group._blocked_recent, [])

    async def test_snapshot_refreshes_immediately_on_entry_and_exit(self):
        self.engine.enabled = True
        with patch.object(self.engine, "_log"), patch.object(self.group, "flat_phase", return_value="scanning"):
            self.group._notify_updates(datetime(2026, 11, 11, 11, 21, 0))
            self.assertEqual(self.group.snapshot["time"], "11:21:00")
            self.engine._position = self.position()
            self.group._notify_updates(datetime(2026, 11, 11, 11, 22, 7))
            self.assertEqual(self.group.snapshot["time"], "11:22:07")
            self.group._notify_updates(datetime(2026, 11, 11, 11, 23, 0))
            self.assertEqual(self.group.snapshot["time"], "11:22:07")
            self.engine._position = None
            self.group._notify_updates(datetime(2026, 11, 11, 11, 24, 9))
            self.assertEqual(self.group.snapshot["time"], "11:24:09")

    async def test_condition_toggles_gate_entries(self):
        m = {"checks": {"CALL": [
            {"key": "news", "gate": True, "enabled": True, "pass": True},
            {"key": "gex_negative", "enabled": True, "pass": False},
        ]}}
        self.assertEqual(Algo2Group.entry_blockers(m, "CALL"), ["score 0/1 < 1"])
        m["checks"]["CALL"][1]["enabled"] = False
        self.assertEqual(Algo2Group.entry_blockers(m, "CALL"), [])

    async def test_gates_always_required_and_score_threshold(self):
        gates = [{"key": k, "gate": True, "enabled": True, "pass": True} for k in sorted(engine_module.GATE_CONDITIONS)]
        score_keys = [k for k, _ in engine_module.CONDITIONS if k not in engine_module.GATE_CONDITIONS]
        self.assertEqual(len(score_keys), 10)
        scored = [{"key": k, "enabled": True, "pass": i < 6} for i, k in enumerate(score_keys)]
        m = {"checks": {"CALL": gates + scored}}
        self.assertEqual(Algo2Group.entry_blockers(m, "CALL", 6), [])
        self.assertEqual(Algo2Group.entry_blockers(m, "CALL", 7), ["score 6/10 < 7"])
        scored[5]["pass"] = False
        self.assertEqual(Algo2Group.entry_blockers(m, "CALL", 6), ["score 5/10 < 6"])
        for c in scored:
            c["pass"] = True
        gates[0]["pass"] = False
        self.assertEqual(Algo2Group.entry_blockers(m, "CALL", 6), [gates[0]["key"]])

    async def test_new_defaults_and_min_score_setting(self):
        cfg = self.engine.cfg
        self.assertEqual((cfg.min_score, cfg.oi_velocity_spike, cfg.vwap_band_sd), (7, 10.0, 1.0))
        self.assertEqual((cfg.trade_windows, cfg.entry_cutoff, cfg.exit_time), ("09:45-15:00", "15:00", "15:15"))
        self.assertFalse(any((cfg.data_sl, cfg.stop_after_sl, cfg.trade_limit, cfg.oi_wall_target,
                              cfg.partial_booking, cfg.max_hold_minutes, cfg.protective_sl)))
        today = algo_module.now_ist().date().isoformat()
        with patch.object(self.group, "signals_used", return_value=9):
            self.assertFalse(self.group.trades_done(today))
            self.engine.set_config({"trade_limit": True})
            self.assertTrue(self.group.trades_done(today))
        self.engine.set_config({"min_score": 8, "conditions": {"news": False, "pcr_filter": False}})
        self.assertEqual(self.engine.cfg.min_score, 8)
        self.engine.set_config({"protective_sl": True})
        self.assertFalse(self.engine.cfg.protective_sl)
        self.assertTrue(self.engine.cfg.conditions["news"])         # gates cannot be switched off
        self.assertFalse(self.engine.cfg.conditions["pcr_filter"])
        for bad in (11, -1, 2.5):
            with self.assertRaises(HTTPException):
                self.engine.set_config({"min_score": bad})

    async def test_old_saved_config_migrates_to_v4(self):
        saved = {**engine_module.asdict(self.engine.cfg), "oi_velocity_spike": 30.0, "vwap_band_sd": 1.5,
                 "min_score": 6, "trade_windows": "09:45-11:15,13:30-15:00", "data_sl": True,
                 "stop_after_sl": True, "partial_booking": True, "max_hold_minutes": 20}
        saved.pop("config_version")
        with patch.object(self.engine._store, "load_state", return_value={"config": saved}), \
                patch.object(self.engine, "_persist") as persist:
            self.engine._restore()
        cfg = self.engine.cfg
        self.assertEqual((cfg.oi_velocity_spike, cfg.vwap_band_sd, cfg.min_score, cfg.trade_windows,
                          cfg.max_hold_minutes, cfg.config_version), (10.0, 1.0, 7, "09:45-15:00", 0, 4))
        self.assertFalse(any((cfg.data_sl, cfg.stop_after_sl, cfg.partial_booking, cfg.trade_limit,
                              cfg.protective_sl)))
        persist.assert_called()

    async def test_v2_saved_config_migrates_to_v4_keeping_v2_values(self):
        saved = {**engine_module.asdict(self.engine.cfg), "config_version": 2, "oi_velocity_spike": 12.0,
                 "min_score": 6, "data_sl": True, "protective_sl": True}
        with patch.object(self.engine._store, "load_state", return_value={"config": saved}), \
                patch.object(self.engine, "_persist"):
            self.engine._restore()
        cfg = self.engine.cfg
        self.assertEqual((cfg.oi_velocity_spike, cfg.min_score, cfg.data_sl, cfg.protective_sl,
                          cfg.config_version), (12.0, 7, False, False, 4))

    async def test_v3_saved_config_disables_broker_protective_sl(self):
        saved = {**engine_module.asdict(self.engine.cfg), "config_version": 3, "protective_sl": True}
        with patch.object(self.engine._store, "load_state", return_value={"config": saved}), \
                patch.object(self.engine, "_persist") as persist:
            self.engine._restore()
        self.assertFalse(self.engine.cfg.protective_sl)
        self.assertEqual(self.engine.cfg.config_version, 4)
        persist.assert_called_once()

    async def test_breakout_history_records_skip_and_outcome(self):
        hist = self.group.history
        start = datetime(2026, 11, 11, 10, 0, 0)
        m = {"spot": 23520.0, "candle": {"t": 1.0, "high": 23515.0, "low": 23500.0},
             "checks": {"CALL": [], "PUT": []}}
        hist.breakout(start, "NIFTY", "CALL", m, taken=False, blockers=["score 3/10 < 6"])
        hist.breakout(start, "NIFTY", "CALL", m, taken=False, blockers=["score 3/10 < 6"])
        hist.on_tick(start.replace(minute=5), {"NIFTY": {"spot": 23561.0, "checks": {}}})
        hist.on_tick(start.replace(minute=21), {"NIFTY": {"spot": 23530.0, "checks": {}}})
        import json
        with open(f"{self.tmp.name}/history/2026-11-11/breakouts.jsonl", encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh]
        self.assertEqual([r["type"] for r in rows], ["breakout", "outcome"])
        self.assertEqual(rows[1]["firstHit"], "target_2R")
        self.assertEqual(rows[1]["maxFavourablePts"], 41.0)
        self.assertTrue(__import__("os").path.exists(f"{self.tmp.name}/history/2026-11-11/metrics.jsonl"))

    async def test_paper_mode_is_separate_and_needs_flat_off(self):
        self.assertTrue(self.group.dry_run)
        self.assertTrue(self.engine._is_dry())
        self.engine._position = self.position()
        with self.assertRaises(HTTPException):
            self.group.set_mode(False)
        self.engine._position = None
        self.group.set_mode(False)
        self.assertFalse(self.engine._is_dry())
        reloaded = Algo2Group(self.tmp.name)
        self.assertFalse(reloaded.dry_run)

    def fake_algo1(self, status="TRADED", **pos_changes):
        algo1 = MagicMock()
        algo1._execution_lock = asyncio.Lock()
        algo1._position = pos = self.position(dry_run=False, execution_state="open", **pos_changes)

        async def exit_locked(p, reason):
            p.exit_order_id, p.execution_state, p.exit_reason = "X1", "exit_pending", reason
        algo1._exit_position_locked = AsyncMock(side_effect=exit_locked)
        algo1._order_state = AsyncMock(return_value={"orderStatus": status})
        algo1._reconcile_locked = AsyncMock()
        return algo1, pos

    async def test_priority_exits_live_algo1_when_algo2_needs_margin(self):
        self.group.dry_run = False
        algo1, pos = self.fake_algo1()
        with patch.dict(algo_module.engines.items, {"master": algo1}), patch.object(self.engine, "_persist"):
            self.assertTrue(await self.engine.free_margin_from_algo1("NIFTY"))
        algo1._exit_position_locked.assert_awaited_once_with(pos, "ALGO2_PRIORITY")
        self.client.place_order.assert_not_awaited()

    async def test_priority_exits_algo1_whose_broker_sl_was_rejected(self):
        # 2026-11-11 child: Algo 1 SL rejected (LPP) -> state "attention" -> priority was skipped.
        self.group.dry_run = False
        algo1, pos = self.fake_algo1(protective_order_id="SL1")
        pos.execution_state = "attention"
        with patch.dict(algo_module.engines.items, {"master": algo1}), patch.object(self.engine, "_persist"):
            self.assertTrue(await self.engine.free_margin_from_algo1("NIFTY"))
        algo1._exit_position_locked.assert_awaited_once_with(pos, "ALGO2_PRIORITY")
        self.client.modify_order.assert_not_called()   # rejected SL cannot be modified: safe exit

    async def test_priority_gives_up_when_algo1_exit_is_rejected(self):
        self.group.dry_run = False
        algo1, _ = self.fake_algo1(status="REJECTED")
        with patch.dict(algo_module.engines.items, {"master": algo1}), patch.object(self.engine, "_persist"):
            self.assertFalse(await self.engine.free_margin_from_algo1("NIFTY"))

    async def test_priority_fast_path_modifies_algo1_stop_to_market_in_one_call(self):
        self.group.dry_run = False
        algo1, pos = self.fake_algo1(protective_order_id="SL1", protective_correlation_id="C1")
        self.client.modify_order = AsyncMock(return_value={"orderStatus": "TRANSIT"})
        self.client.wait_order_event = AsyncMock(return_value=True)
        algo1._order_state = AsyncMock(side_effect=[{"orderStatus": "PENDING"}, {"orderStatus": "TRADED"}])
        with patch.dict(algo_module.engines.items, {"master": algo1}), patch.object(self.engine, "_persist"):
            self.assertTrue(await self.engine.free_margin_from_algo1("NIFTY"))
            await asyncio.sleep(0)
        self.client.modify_order.assert_awaited_once()
        oid, payload = self.client.modify_order.await_args.args
        self.assertEqual((oid, payload["orderType"], payload["quantity"]), ("SL1", "MARKET", pos.quantity))
        algo1._exit_position_locked.assert_not_awaited()      # no cancel + new SELL round trips
        self.client.place_order.assert_not_awaited()
        self.client.wait_order_event.assert_awaited()          # woken by the order stream, not a fixed sleep
        self.assertEqual((pos.exit_order_id, pos.exit_reason, pos.protective_order_id),
                         ("SL1", "ALGO2_PRIORITY", None))
        algo1._reconcile_locked.assert_awaited()               # done in the background

    async def test_priority_fast_path_failure_restores_stop_and_uses_safe_exit(self):
        self.group.dry_run = False
        algo1, pos = self.fake_algo1(protective_order_id="SL1", protective_correlation_id="C1")
        seen = {}

        async def exit_locked(p, reason):
            seen["protective"] = p.protective_order_id
            p.exit_order_id, p.execution_state, p.exit_reason = "X1", "exit_pending", reason
        algo1._exit_position_locked = AsyncMock(side_effect=exit_locked)
        self.client.modify_order = AsyncMock(side_effect=algo_module.DhanAPIError(400, "DH-906", "cannot modify"))
        with patch.dict(algo_module.engines.items, {"master": algo1}), patch.object(self.engine, "_persist"):
            self.assertTrue(await self.engine.free_margin_from_algo1("NIFTY"))
        self.assertEqual(seen["protective"], "SL1")            # safe exit still sees (and cancels) the stop
        algo1._exit_position_locked.assert_awaited_once_with(pos, "ALGO2_PRIORITY")

    async def test_priority_never_touches_algo1_in_paper_or_when_switched_off(self):
        algo1, _ = self.fake_algo1()
        with patch.dict(algo_module.engines.items, {"master": algo1}), patch.object(self.engine, "_persist"):
            self.assertFalse(await self.engine.free_margin_from_algo1("NIFTY"))     # Algo 2 PAPER
            self.group.dry_run = False
            self.engine.cfg.priority_over_algo1 = False
            self.assertFalse(await self.engine.free_margin_from_algo1("NIFTY"))
            self.engine.cfg.priority_over_algo1 = True
            algo1._position.dry_run = True                                          # Algo 1 paper trade
            self.assertFalse(await self.engine.free_margin_from_algo1("NIFTY"))
        algo1._exit_position_locked.assert_not_awaited()

    async def test_dispatch_retries_entry_after_priority_exit(self):
        self.engine.enabled = True
        self.engine._position = None
        calls = []

        async def fake_enter(leg, side):
            calls.append(leg.funds_retries)
            if len(calls) == 1:
                leg.status, leg.note = "skipped", "insufficient balance: Rs100 available"
            else:
                self.engine._position = self.position()

        async def fake_prepare(leg, side):
            leg.prepared_side, leg.security_id, leg.strike = side, 1, 23450.0
        m = {"candle": {"high": 23510.0, "low": 23470.0}, "spot": 23515.0, "triggerUp": 23510.5,
             "triggerDown": 23469.5, "checks": {"CALL": []}}
        with patch.object(self.engine, "_enter", side_effect=fake_enter), \
             patch.object(self.engine, "_prepare_contract", side_effect=fake_prepare), \
             patch.object(self.engine, "free_margin_from_algo1", AsyncMock(return_value=True)) as free, \
             patch.object(self.group.data, "chain_rows", return_value=[]), \
             patch.object(self.engine, "_persist"), patch.object(self.group, "save_settings"):
            await self.group._dispatch("NIFTY", "CALL", m)
        free.assert_awaited_once_with("NIFTY")
        self.assertEqual(calls, [0, 1])
        self.assertIsNotNone(self.engine._position)
        self.assertTrue(self.group.last_signal["consumed"])

    async def test_arm_refused_when_algo1_uses_intraday(self):
        algo1 = algo_module.engines.items["master"]
        with patch.object(algo1.cfg, "product_type", "INTRADAY"):
            result = await self.engine.arm()
        self.assertFalse(self.engine.enabled)
        self.assertIn("MARGIN", result["armError"]["reason"])

    async def test_orders_are_tagged_algo2_and_intraday(self):
        self.assertEqual(self.engine.ORDER_TAG, "AL2")
        self.assertEqual(self.engine.cfg.product_type, "INTRADAY")
        with patch.object(self.engine, "_persist"):
            with self.assertRaises(HTTPException):
                self.engine.set_config({"t1_pct": 150})
            self.engine.set_config({"product_type": "MARGIN", "t1_pct": 60})
        self.assertEqual(self.engine.cfg.product_type, "INTRADAY")
        self.assertEqual(self.engine.cfg.t1_pct, 60)

    def test_raw_chain_snapshot_once_per_minute_and_journals_never_pruned(self):
        hist = self.group.history
        now = datetime(2026, 11, 10, 10, 0, 5)
        hist.chain_snapshot(now, "NIFTY", 23500.0, 23560.0, "2026-11-17", CHAIN)
        hist.chain_snapshot(now.replace(second=40), "NIFTY", 23501.0, 23561.0, "2026-11-17", CHAIN)
        hist.chain_snapshot(now.replace(minute=1), "NIFTY", 23502.0, 23562.0, "2026-11-17", CHAIN)
        rows = daily_cache._read_jsonl(os.path.join(hist.dir, "2026-11-10", "chain.jsonl"))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["strikes"][2], {"k": 23500, "ce": {"ltp": 100.0, "oi": 3000, "prevOi": None,
                         "vol": None, "iv": None, "d": 0.5, "g": 0.004, "t": None, "v": None},
                         "pe": {"ltp": 90.0, "oi": 3000, "prevOi": None, "vol": None, "iv": None, "d": -0.5,
                                "g": 0.002, "t": None, "v": None}})
        store = self.engine._store
        store.append_event({"event": "old"}, day="2000-01-01")
        self.assertEqual(store.prune_journals(), 0)
        self.assertIn("2000-01-01", store.journal_days())


class DailyCacheTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = MagicMock(futures={"NIFTY": {"securityId": 61471, "segment": "NSE_FNO"}},
                              ensure_futures=AsyncMock())
        self.trades = MagicMock()
        self.trades.read_trades.return_value = [
            {"date": "2026-11-10", "premiumPnl": 1200.0, "exitReason": "T1_FULL"},
            {"date": "2026-11-09", "premiumPnl": -500.0, "exitReason": "SL"}]
        self.client = MagicMock()
        self.client.place_order = AsyncMock(side_effect=AssertionError("cache must never trade"))
        self.client.get_intraday = AsyncMock(return_value={
            "timestamp": [1, 2], "open": [10, 11], "high": [12, 13], "low": [9, 10], "close": [11, 12],
            "volume": [5, 6], "open_interest": [100, 120]})
        patcher = patch.object(daily_cache, "get_data_client", return_value=self.client)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sleep = AsyncMock()
        self.cache = daily_cache.Algo2DailyCache(self.tmp.name, self.data, lambda: {"master": self.trades},
                                                 sleep=self.sleep)

    def write(self, day, name, rows):
        os.makedirs(os.path.join(self.tmp.name, day), exist_ok=True)
        with open(os.path.join(self.tmp.name, day, f"{name}.jsonl"), "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")

    async def test_after_close_saves_candles_summary_once(self):
        self.write("2026-11-10", "breakouts", [
            {"type": "breakout", "index": "NIFTY", "side": "CALL", "taken": False,
             "blockers": ["news", "score 4/10 < 6"], "score": {"CALL": {"passed": 4, "total": 10}}},
            {"type": "outcome", "taken": False, "firstHit": "target_2R"}])
        in_market = datetime(2026, 11, 10, 14, 0)
        self.assertFalse(self.cache.due(in_market))   # Master quota stays free for live trading
        after = datetime(2026, 11, 10, 15, 35)
        self.assertEqual(self.cache.pending_days(after), ["2026-11-10"])
        task = self.cache.schedule(after, ["NIFTY"])
        await task
        # NIFTY spot + NIFTY fut + VIX + 6 heavyweights, 1-min candles, 3 s apart
        self.assertEqual(self.client.get_intraday.await_count, 9)
        first = self.client.get_intraday.await_args_list[0].args
        self.assertEqual(first[2:], ("INDEX", "1", "2026-11-10 09:15:00", "2026-11-10 15:30:00"))
        fut_call = next(c for c in self.client.get_intraday.await_args_list if c.args[2] == "FUTIDX")
        self.assertTrue(fut_call.kwargs["oi"])
        self.assertEqual(self.sleep.await_count, 9)
        folder = os.path.join(self.tmp.name, "2026-11-10")
        fut = daily_cache._read_json(os.path.join(folder, "candles", "NIFTY_fut.json"))
        self.assertEqual([c["oi"] for c in fut["candles"]], [100.0, 120.0])
        summary = daily_cache._read_json(os.path.join(folder, "day_summary.json"))
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(summary["scoreDistribution"], {"4/10": 1})
        self.assertEqual(summary["blockers"], {"news": 1, "score_below_min": 1})
        self.assertEqual(summary["skippedWouldHit2R"], 1)
        self.assertEqual(summary["trades"]["master"], {"count": 1, "premiumPnl": 1200.0,
                                                       "exitReasons": {"T1_FULL": 1}})
        self.assertEqual(summary["candles"]["RELIANCE"], 2)
        self.assertTrue(self.cache.is_done("2026-11-10"))
        self.assertIsNone(self.cache.schedule(datetime(2026, 11, 10, 15, 50), ["NIFTY"]))
        self.client.place_order.assert_not_awaited()

    async def test_rate_limit_stops_and_retries_later_then_catches_up(self):
        self.write("2026-11-09", "reports", [{"x": 1}])
        self.client.get_intraday.side_effect = [
            {"timestamp": [1], "open": [1], "high": [2], "low": [1], "close": [2]},
            DhanAPIError(429, "DH-904", "Too many requests")]
        morning = datetime(2026, 11, 10, 9, 5)        # pre-market catch-up of a missed day
        self.assertEqual(self.cache.pending_days(morning), ["2026-11-09"])
        await self.cache.schedule(morning, ["NIFTY"])
        self.assertFalse(self.cache.is_done("2026-11-09"))
        self.assertIn("rate limited", self.cache.error)
        self.assertFalse(self.cache.due(morning))     # backs off
        self.cache._next_try = 0
        self.client.get_intraday.side_effect = None
        await self.cache.schedule(morning, ["NIFTY"])
        self.assertTrue(self.cache.is_done("2026-11-09"))
        self.assertEqual(self.client.get_intraday.await_count, 2 + 8)   # resumed, saved file not re-fetched


if __name__ == "__main__":
    unittest.main()
