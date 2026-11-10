import tempfile
import time
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException

import app.algo as algo_module
from app.algo import DayLeg
from app.algo2 import calculations as calc
from app.algo2 import engine as engine_module
from app.algo2.engine import Algo2Group, Algo2Position, parse_windows, validate_algo2_config
from app.algo2.market_data import Series, parse_candles, resolve_futures_from_csv
from app.algo2.news_events import NewsEventFetcher


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

    async def test_twenty_minute_rule_exits_when_t1_not_hit(self):
        self.engine._position = self.position(entry_confirmed_ts=time.time() - 20 * 60 - 1, spot=23510.0)
        await self.engine._manage_position()
        self.assertEqual(self.engine._today_trades[-1]["exitReason"], "STAGNANT_20M")

    async def test_data_sl_exits_on_call_oi_trap(self):
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

    async def test_stop_loss_exit_ends_entries_for_the_day(self):
        today = algo_module.now_ist().date().isoformat()
        self.assertIsNone(self.group.sl_hit_today(today))
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
            "checks": {"CALL": [{"key": "news", "enabled": True, "pass": True},
                                {"key": "gex_negative", "enabled": True, "pass": False}],
                       "PUT": [{"key": "news", "enabled": True, "pass": True}]}}
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
        self.assertIn("fail: Net GEX negative", reports[0])
        self.assertIn("PUT 1/1 OK - READY", reports[0])
        self.assertEqual(len(self.group.reports), 1)
        self.assertEqual(len(logs), 1)          # nothing else logged between reports

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
            {"key": "news", "enabled": True, "pass": True},
            {"key": "gex_negative", "enabled": True, "pass": False},
        ]}}
        self.assertEqual(Algo2Group.entry_blockers(m, "CALL"), ["gex_negative"])
        m["checks"]["CALL"][1]["enabled"] = False
        self.assertEqual(Algo2Group.entry_blockers(m, "CALL"), [])

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


if __name__ == "__main__":
    unittest.main()
