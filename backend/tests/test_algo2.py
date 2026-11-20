import asyncio
import json
import os
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException

import app.algo as algo_module
from app.algo import DayLeg
from app.algo2 import calculations as calc
from app.algo2 import daily_cache
from app.algo2 import engine as engine_module
from app.algo2 import microstructure as micro
from app.algo2 import score_v5 as v5
from app.algo2.engine import Algo2Group, Algo2Position, parse_windows, validate_algo2_config
from app.algo2.market_data import Algo2MarketData, Series, parse_candles, resolve_futures_from_csv
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


class ScoreV5Tests(unittest.TestCase):
    def candles(self, day, n, start=(9, 15)):
        t0 = datetime(day.year, day.month, day.day, *start, tzinfo=v5.IST).timestamp()
        return [{"t": t0 + i * 300, "open": 100 + i, "high": 110 + i, "low": 90 + i, "close": 105 + i}
                for i in range(n)]

    def test_points_missing_data_earns_nothing(self):
        feats = {k: None for k in v5.FEATURES}
        self.assertEqual(v5.points(feats), 0)
        feats.update(oi_unwinding=True, vwap_band=False, basis=True, f08_vix_low=False)
        self.assertEqual(v5.points(feats), 2)
        self.assertEqual(len(v5.FEATURES), 24)

    def test_fifteen_minute_stop_candle_needs_all_parts(self):
        day = date(2026, 11, 16)
        c = self.candles(day, 12)                       # 09:15 .. 10:10 closed 5-min candles
        now = datetime(2026, 11, 16, 10, 15, 2, tzinfo=v5.IST)
        got = v5.last_closed_candle(c, now, 15)
        self.assertEqual((got["open"], got["high"], got["low"], got["close"]), (109, 121, 99, 116))
        self.assertIsNone(v5.last_closed_candle(c[:-1], now, 15))   # 10:10 bar not published yet
        self.assertEqual(v5.last_closed_candle(c[:-1], now.replace(minute=13), 15)["low"], 96)
        self.assertIs(v5.last_closed_candle(c, now, 5), c[-1])

    def test_orb_range_and_max_pain(self):
        day = date(2026, 11, 16)
        c = self.candles(day, 9)
        now = datetime(2026, 11, 16, 10, 0, tzinfo=v5.IST)
        self.assertEqual(v5.range_between(c, now, 555, 585), (115, 90))
        self.assertIsNone(v5.range_between(c[:5], now, 555, 585))
        rows = [{"strike": k, "ce": {"prevOi": ce}, "pe": {"prevOi": pe}}
                for k, ce, pe in ((23400, 10, 900), (23450, 50, 500), (23500, 300, 300), (23550, 600, 40),
                                  (23600, 900, 10))]
        self.assertEqual(v5.max_pain(rows), 23500.0)

    def test_participant_and_global_cues(self):
        csv_text = ('"Participant wise Open Interest"\nClient Type,Future Index Long,Future Index Short\n'
                    "Client,150000,90000\nDII,1,2\n")
        self.assertEqual(v5.parse_participant(csv_text), 60000.0)
        self.assertIsNone(v5.parse_participant("<html>blocked</html>"))
        utc = timezone.utc
        series = [(datetime(2026, 11, d, 21, 0, tzinfo=utc).timestamp(), c)
                  for d, c in ((11, 100.0), (12, 102.0), (13, 101.0))]
        self.assertAlmostEqual(v5.daily_change(series, date(2026, 11, 16)), (101 - 102) / 102 * 100)
        es = [(datetime(2026, 11, 13, 9, 0, tzinfo=utc).timestamp(), 6000.0),
              (datetime(2026, 11, 13, 10, 0, tzinfo=utc).timestamp(), 6100.0),
              (datetime(2026, 11, 16, 3, 0, tzinfo=utc).timestamp(), 6060.0),
              (datetime(2026, 11, 16, 4, 0, tzinfo=utc).timestamp(), 7000.0)]
        self.assertAlmostEqual(v5.es_overnight(es, date(2026, 11, 13), date(2026, 11, 16)), 1.0)

    def test_features_side_mirroring(self):
        now = datetime(2026, 11, 16, 10, 7, tzinfo=v5.IST)
        c = self.candles(now.date(), 10)
        ctx = {"prevDay": {"high": 120.0, "low": 80.0, "close": 100.0}, "vixOpens": [15.0] * 20,
               "clientChange": -5000.0, "global": {"esOvernight": 0.5, "spx": -0.2, "usvix": 3.0, "inr": -0.1}}
        kw = dict(now=now, entry=130.0, spot=130.0, ctx=ctx, candle5=c[-1], spot_candles=c, fut1m=[],
                  fut_ltp=None, vwap5=None, rows=[], near=[], oi_before=None, maxpain=110.0,
                  expiry=date(2026, 11, 17), vix_open=14.0, bank_now=50100.0, bank_15=50000.0,
                  base_checks={"oi_unwinding": True, "vwap_band": False, "basis": True})
        call, put = v5.compute_features("CALL", **kw), v5.compute_features("PUT", **kw)
        self.assertTrue(call["x_orb_break"] and call["f06_prev_break"] and call["f02_day_dir"])
        self.assertFalse(put["x_orb_break"] or put["f06_prev_break"] or put["f02_day_dir"])
        self.assertEqual((call["f16_toward_maxpain"], put["f16_toward_maxpain"]), (False, True))
        self.assertEqual((call["f17_client_contra"], put["f17_client_contra"]), (True, False))
        self.assertEqual((call["f22_usvix_down"], put["f22_usvix_down"]), (False, True))
        self.assertTrue(call["f08_vix_low"] and call["f09_dte_le1"])
        self.assertIsNone(call["x_flow_delta"])
        self.assertIsNone(call["f03_vwap_side"])


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


class CandleFetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_candles_fetch_from_0914_so_the_0915_minute_is_kept(self):
        # Dhan's fromDate is exclusive: from 09:15:00 the 09:15 bar opened at the 09:16 price (16-Nov).
        now = datetime(2026, 11, 16, 9, 25, 4, tzinfo=v5.IST)
        t915 = now.replace(minute=15, second=0).timestamp()
        bars = {"timestamp": [t915 - 60, t915, t915 + 300], "open": [1, 25206.15, 25250.8],
                "high": [1, 25310.7, 25268.1], "low": [1, 25195.35, 25237.05], "close": [1, 25250.0, 25260.0]}
        client = MagicMock()
        client.get_intraday = AsyncMock(return_value=bars)
        with tempfile.TemporaryDirectory() as tmp, \
                patch("app.algo2.market_data.get_data_client", return_value=client):
            data = Algo2MarketData(tmp)
            data.futures["NIFTY"] = {"securityId": 61471, "segment": "NSE_FNO"}
            await data.refresh_candles("NIFTY", now)
            await data.refresh_fut_1m("NIFTY", now)
        froms = {c.args[4] for c in client.get_intraday.await_args_list}
        self.assertEqual(froms, {"2026-11-16 09:14:00"})
        self.assertEqual([c["t"] for c in data.spot_candles["NIFTY"]], [t915, t915 + 300])
        self.assertEqual(data.spot_candles["NIFTY"][0]["open"], 25206.15)
        self.assertEqual(data.fut_1m["NIFTY"][0]["t"], t915)


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

    def test_manual_off_lasts_only_for_the_day_it_was_saved(self):
        today = algo_module.now_ist().date()
        path = os.path.join(self.tmp.name, "group.json")
        for saved_day, expected in ((today.isoformat(), False), ((today - timedelta(days=1)).isoformat(), True),
                                    (None, True)):
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"dryRun": False, "enabled": False, "savedDay": saved_day}, fh)
            self.assertEqual(Algo2Group(self.tmp.name).saved_enabled, expected, saved_day)

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

    async def test_alerts_show_readable_gate_and_today_total(self):
        checks = [{"key": "time_window", "gate": True, "enabled": True, "pass": False},
                  {"key": "oi_unwinding", "gate": False, "enabled": True, "pass": True}]
        self.group.metrics["NIFTY"] = {"spot": 25000.0, "triggerUp": 25010.0, "triggerDown": 24990.0,
                                       "checks": {"CALL": checks, "PUT": checks}}
        self.assertIn("CALL blocked: outside entry time (09:45-15:00)", self.group._index_line("NIFTY"))
        status = self.group._status_telegram()
        self.assertIn("\nCE buy above 25,010.0 | PE buy below 24,990.0\n", status)
        self.assertNotIn("Inside a trade window", status)
        self.engine._today_trades = [{"date": self.engine._day, "premiumPnl": 500.0}]
        self.engine._position = pos = self.position(lots=1, quantity=65)
        pos.spot, pos.premium = 23541.0, 120.0
        with self.assertLogs("dhan.algo", level="INFO") as logs:
            await self.engine._manage_position()
        exit_alert = [r.telegram_detail for r in logs.records
                      if getattr(r, "telegram_event", "") == "algo_exit"][-1]
        self.assertIn("P/L: +Rs 1,300", exit_alert)
        self.assertIn("Today total P/L: +Rs 1,800 (2 trades)", exit_alert)

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

    async def test_expiry_day_target_is_1r(self):
        self.group.signal_ctx["NIFTY"] = {"candleLow": 23470.0, "candleHigh": 23510.0, "wall": None}
        today = algo_module.now_ist().date().isoformat()
        with patch.object(self.engine, "_delta_for", return_value=0.7), \
                patch.object(self.group.data, "chain_expiry", return_value=today):
            target, stop = self.engine._levels(DayLeg("NIFTY"), "CALL", 23500.0, 20.0,
                                               {"quantity": 65, "balance": 100000})
        self.assertEqual((target, stop), (23530.0, 23470.0))

    async def test_expiry_eve_uses_trading_days(self):
        g = self.group
        self.assertTrue(g.is_expiry_eve(date(2026, 11, 17), date(2026, 11, 16)))    # Mon before Tue expiry
        self.assertTrue(g.is_expiry_eve(date(2026, 11, 23), date(2026, 11, 20)))    # 24-Nov holiday: Fri before Mon
        self.assertTrue(g.is_expiry_eve(date(2026, 1, 27), date(2026, 1, 23)))      # 26-Jan Mon holiday
        self.assertFalse(g.is_expiry_eve(date(2026, 11, 17), date(2026, 11, 17)))   # expiry day itself
        self.assertFalse(g.is_expiry_eve(date(2026, 11, 24), date(2026, 11, 19)))
        g.cfg.skip_expiry_eve = False
        self.assertFalse(g.is_expiry_eve(date(2026, 11, 17), date(2026, 11, 16)))

    async def test_v5_config_moves_to_v6_plan(self):
        saved = {**engine_module.asdict(self.engine.cfg), "config_version": 5, "min_score": 18}
        for k in ("skip_expiry_eve", "target_r", "expiry_target_r"):
            saved.pop(k)
        with patch.object(self.engine._store, "load_state", return_value={"config": saved}), \
                patch.object(self.engine, "_persist") as persist:
            self.engine._restore()
        cfg = self.engine.cfg
        self.assertEqual((cfg.skip_expiry_eve, cfg.target_r, cfg.expiry_target_r, cfg.min_score,
                          cfg.config_version), (True, 2.0, 1.0, 18, 6))
        persist.assert_called()

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
        self.engine.cfg.score_model = "classic"
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
        self.assertEqual((cfg.min_score, cfg.oi_velocity_spike, cfg.vwap_band_sd), (17, 10.0, 1.0))
        self.assertEqual((cfg.score_model, cfg.indices, cfg.sl_candle_minutes, cfg.expiry_block),
                         ("v5", ["NIFTY"], 15, "10:45-11:45"))
        self.assertTrue(cfg.first_touch_only and cfg.atm_fallback and cfg.max_affordable)
        self.assertEqual((cfg.trade_windows, cfg.entry_cutoff, cfg.exit_time), ("09:45-15:00", "15:00", "15:15"))
        self.assertFalse(any((cfg.data_sl, cfg.stop_after_sl, cfg.trade_limit, cfg.oi_wall_target,
                              cfg.partial_booking, cfg.max_hold_minutes, cfg.protective_sl)))
        today = algo_module.now_ist().date().isoformat()
        with patch.object(self.group, "signals_used", return_value=9):
            self.assertFalse(self.group.trades_done(today))
            self.engine.set_config({"trade_limit": True})
            self.assertTrue(self.group.trades_done(today))
        self.engine.set_config({"min_score": 18, "conditions": {"expiry_block": False, "f19_bank_15m": False}})
        self.assertEqual(self.engine.cfg.min_score, 18)
        self.engine.set_config({"protective_sl": True})
        self.assertFalse(self.engine.cfg.protective_sl)
        self.assertTrue(self.engine.cfg.conditions["expiry_block"])   # gates cannot be switched off
        self.assertFalse(self.engine.cfg.conditions["f19_bank_15m"])
        for bad in (25, -1, 2.5):
            with self.assertRaises(HTTPException):
                self.engine.set_config({"min_score": bad})
        for bad in ({"sl_candle_minutes": 7}, {"score_model": "x"}, {"expiry_block": "11:45-10:45"}):
            with self.assertRaises(HTTPException):
                self.engine.set_config(bad)
        self.engine.set_config({"score_model": "classic", "min_score": 8})
        with self.assertRaises(HTTPException):
            self.engine.set_config({"min_score": 11})   # classic scores only 10 conditions

    async def test_old_saved_config_migrates_to_v5(self):
        saved = {**engine_module.asdict(self.engine.cfg), "oi_velocity_spike": 30.0, "vwap_band_sd": 1.5,
                 "min_score": 6, "trade_windows": "09:45-11:15,13:30-15:00", "data_sl": True,
                 "stop_after_sl": True, "partial_booking": True, "max_hold_minutes": 20}
        saved.pop("config_version")
        with patch.object(self.engine._store, "load_state", return_value={"config": saved}), \
                patch.object(self.engine, "_persist") as persist:
            self.engine._restore()
        cfg = self.engine.cfg
        self.assertEqual((cfg.oi_velocity_spike, cfg.vwap_band_sd, cfg.min_score, cfg.trade_windows,
                          cfg.max_hold_minutes, cfg.config_version), (10.0, 1.0, 17, "09:45-15:00", 0, 6))
        self.assertFalse(any((cfg.data_sl, cfg.stop_after_sl, cfg.partial_booking, cfg.trade_limit,
                              cfg.protective_sl)))
        persist.assert_called()

    async def test_v2_saved_config_migrates_to_v5_keeping_v2_values(self):
        saved = {**engine_module.asdict(self.engine.cfg), "config_version": 2, "oi_velocity_spike": 12.0,
                 "min_score": 6, "data_sl": True, "protective_sl": True}
        with patch.object(self.engine._store, "load_state", return_value={"config": saved}), \
                patch.object(self.engine, "_persist"):
            self.engine._restore()
        cfg = self.engine.cfg
        self.assertEqual((cfg.oi_velocity_spike, cfg.min_score, cfg.data_sl, cfg.protective_sl,
                          cfg.config_version), (12.0, 17, False, False, 6))

    async def test_v3_saved_config_disables_broker_protective_sl(self):
        saved = {**engine_module.asdict(self.engine.cfg), "config_version": 3, "protective_sl": True}
        with patch.object(self.engine._store, "load_state", return_value={"config": saved}), \
                patch.object(self.engine, "_persist") as persist:
            self.engine._restore()
        self.assertFalse(self.engine.cfg.protective_sl)
        self.assertEqual(self.engine.cfg.config_version, 6)
        persist.assert_called()

    async def test_v4_live_config_moves_to_v5_rules(self):
        saved = {**engine_module.asdict(self.engine.cfg), "config_version": 4, "indices": ["NIFTY", "SENSEX"],
                 "score_model": "classic", "min_score": 7, "first_touch_only": False, "atm_fallback": False,
                 "max_affordable": False, "conditions": {"pcr_filter": False}}
        for k in ("sl_candle_minutes", "expiry_block"):
            saved.pop(k)
        with patch.object(self.engine._store, "load_state", return_value={"config": saved}), \
                patch.object(self.engine, "_persist") as persist:
            self.engine._restore()
        cfg = self.engine.cfg
        self.assertEqual((cfg.indices, cfg.score_model, cfg.min_score, cfg.sl_candle_minutes, cfg.expiry_block,
                          cfg.exit_time, cfg.config_version), (["NIFTY"], "v5", 17, 15, "10:45-11:45", "15:15", 6))
        self.assertTrue(cfg.first_touch_only and cfg.atm_fallback and cfg.max_affordable)
        self.assertTrue(all(cfg.conditions[k] for k in v5.FEATURES))
        persist.assert_called()
        persist.reset_mock()
        saved = {**engine_module.asdict(cfg), "min_score": 18}
        with patch.object(self.engine._store, "load_state", return_value={"config": saved}), \
                patch.object(self.engine, "_persist") as persist:
            self.engine._restore()
        self.assertEqual(self.engine.cfg.min_score, 18)   # operator edits after v5 are kept
        persist.assert_not_called()

    def v5_metrics(self, side="CALL", points=17, stop_low=23480.0, bar=1000.0):
        feats = [{"key": k, "gate": False, "enabled": True, "pass": i < points} for i, k in enumerate(v5.FEATURES)]
        gates = [{"key": k, "gate": True, "enabled": True, "pass": True} for k in v5.GATES]
        return {"spot": 23520.0, "breakout": side, "candle": {"t": bar, "high": 23515.0, "low": 23500.0},
                "triggerUp": 23515.5, "triggerDown": 23499.5,
                "checks": {"CALL": gates + feats, "PUT": gates + feats},
                "v5": {"stopCandle": {"high": 23530.0, "low": stop_low},
                       "stopDist": {"CALL": 23520.0 - stop_low, "PUT": 10.0}}}

    async def run_tick(self, m, busy=False, dispatch_effect=None):
        g = self.group
        g.cfg.indices = ["NIFTY"]
        with patch.object(engine_module, "now_ist", return_value=datetime(2026, 11, 16, 10, 7, 30,
                                                                         tzinfo=algo_module.IST)), \
                patch.object(g, "_data_needed", return_value=True), \
                patch.object(g.data, "ensure_futures", new_callable=AsyncMock), \
                patch.object(g.news, "refresh_nse", new_callable=AsyncMock), \
                patch.object(g.data, "poll_quotes", new_callable=AsyncMock), \
                patch.object(g.data, "refresh_candles", new_callable=AsyncMock), \
                patch.object(g.data, "refresh_fut_1m", new_callable=AsyncMock), \
                patch.object(g.v5ctx, "schedule"), patch.object(g, "_presubscribe"), \
                patch.object(g, "compute_metrics", return_value=m), \
                patch.object(g, "has_position", return_value=busy), \
                patch.object(g, "dispatch", new_callable=AsyncMock, side_effect=dispatch_effect) as dispatch, \
                patch.object(g, "_note_blocked") as blocked:
            g.master.enabled = True
            await g.tick()
        return dispatch, blocked

    async def test_v5_first_touch_only_and_touches_while_holding_are_spent(self):
        dispatch, _ = await self.run_tick(self.v5_metrics(), busy=True)
        dispatch.assert_not_awaited()
        dispatch, _ = await self.run_tick(self.v5_metrics())        # same candle after the exit: not re-entered
        dispatch.assert_not_awaited()
        dispatch, _ = await self.run_tick(self.v5_metrics(bar=1300.0))
        dispatch.assert_awaited_once()
        self.group._attempted.clear()
        dispatch, _ = await self.run_tick(self.v5_metrics(bar=1300.0))   # second touch of the same candle
        dispatch.assert_not_awaited()

    async def test_v5_blocked_first_touch_is_not_retried(self):
        dispatch, blocked = await self.run_tick(self.v5_metrics(points=16))
        dispatch.assert_not_awaited()
        self.assertIn("score 16/24 < 17", blocked.call_args.args[3])
        dispatch, blocked = await self.run_tick(self.v5_metrics(points=20))   # score improves later: still spent
        dispatch.assert_not_awaited()
        blocked.assert_not_called()

    async def test_v5_waits_for_stop_candle_and_rejects_wrong_side_stop(self):
        m = self.v5_metrics()
        m["v5"]["stopCandle"] = None
        dispatch, blocked = await self.run_tick(m)
        dispatch.assert_not_awaited()
        blocked.assert_not_called()                                   # not spent: re-evaluated next second
        dispatch, blocked = await self.run_tick(self.v5_metrics(stop_low=23525.0))
        dispatch.assert_not_awaited()
        self.assertIn("15-min SL candle on the wrong side", blocked.call_args.args[3])

    async def test_v5_premium_not_ready_keeps_touch_for_retry(self):
        async def not_ready(key, side, m):
            self.group._attempted.pop(key, None)                      # what _dispatch does on a stale premium
        dispatch, _ = await self.run_tick(self.v5_metrics(), dispatch_effect=not_ready)
        dispatch.assert_awaited_once()
        dispatch, _ = await self.run_tick(self.v5_metrics())
        dispatch.assert_awaited_once()                                # same touch retried
        self.group._attempted.clear()
        dispatch, _ = await self.run_tick(self.v5_metrics())
        dispatch.assert_not_awaited()                                 # a real dispatch spends the touch

    async def test_v5_missing_futures_data_earns_no_points(self):
        g = self.group
        now = datetime(2026, 11, 16, 10, 0, 5, tzinfo=algo_module.IST)
        t0 = now.replace(hour=9, minute=15, second=0).timestamp()
        g.data.spot_candles["NIFTY"] = [{"t": t0 + i * 300, "open": 23400.0, "high": 23420.0, "low": 23390.0,
                                         "close": 23410.0} for i in range(9)]
        with patch.object(g.data, "spot", return_value=23500.0), \
                patch.object(g.data, "chain_rows", return_value=CHAIN), \
                patch.object(g.data, "futures_price", return_value=None):
            m = g.compute_metrics("NIFTY", now)
        for side in ("CALL", "PUT"):
            feats = m["v5"]["features"][side]
            self.assertEqual((feats["vwap_band"], feats["basis"], feats["oi_unwinding"]), (None, None, None))
            self.assertEqual(m["score"][side]["passed"], v5.points(feats))
            self.assertEqual(m["score"][side]["total"], 24)
        self.assertEqual(m["v5"]["stopCandle"]["low"], 23390.0)
        self.assertFalse(m["v5"]["expiryEve"])

    async def test_v5_expiry_eve_gate_blocks_entries(self):
        g = self.group
        now = datetime(2026, 11, 16, 10, 0, 5, tzinfo=algo_module.IST)
        with patch.object(g.data, "spot", return_value=23500.0), \
                patch.object(g.data, "chain_rows", return_value=CHAIN), \
                patch.object(g.data, "chain_expiry", return_value="2026-11-17"), \
                patch.object(g.data, "futures_price", return_value=None):
            m = g.compute_metrics("NIFTY", now)
        self.assertTrue(m["v5"]["expiryEve"])
        self.assertIn("expiry_eve", m["score"]["CALL"]["gatesFailed"])
        self.assertIn("expiry_eve", Algo2Group.entry_blockers(m, "CALL", 0))

    async def test_market_calendar_matches_start_gate_list(self):
        import importlib.util
        from app import market_calendar
        path = os.path.join(os.path.dirname(__file__), "..", "..", "deployment", "aws", "market_holidays.py")
        spec = importlib.util.spec_from_file_location("deploy_market_holidays", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(market_calendar.NSE_TRADING_HOLIDAYS, mod.NSE_TRADING_HOLIDAYS)

    async def test_v5_features_are_scored_at_the_trigger_not_the_later_spot(self):
        # Live 16-Nov 11:01: spot ticked past the trigger; the backtest scores at the trigger level.
        g = self.group
        now = datetime(2026, 11, 16, 10, 0, 5, tzinfo=algo_module.IST)
        t0 = now.replace(hour=9, minute=15, second=0).timestamp()
        g.data.spot_candles["NIFTY"] = [{"t": t0 + i * 300, "open": 23400.0, "high": 23420.0, "low": 23390.0,
                                         "close": 23410.0} for i in range(9)]
        with patch.object(g.data, "spot", return_value=23500.0), \
                patch.object(g.data, "chain_rows", return_value=CHAIN), \
                patch.object(g.data, "futures_price", return_value=None):
            m = g.compute_metrics("NIFTY", now)
        self.assertEqual(m["triggerUp"], 23420.5)
        self.assertEqual(m["v5"]["stopDist"]["CALL"], 30.5)           # 23420.5 - 23390, not 23500 - 23390
        self.assertTrue(m["v5"]["features"]["CALL"]["f10_wall_room"])  # wall 23600 >= 2 x 30.5 from the trigger

    async def test_no_breakout_on_previous_candle_before_new_bar_is_published(self):
        # Live 11-Nov 09:45:01: Dhan had not published 09:40-09:45 yet, so 09:35-09:40 high triggered a trade.
        g = self.group
        now = datetime(2026, 11, 16, 10, 5, 2, tzinfo=algo_module.IST)
        t0 = now.replace(hour=9, minute=15, second=0).timestamp()
        g.data.spot_candles["NIFTY"] = [{"t": t0 + i * 300, "open": 23400.0, "high": 23420.0, "low": 23390.0,
                                         "close": 23410.0} for i in range(9)]   # last bar 09:55-10:00
        with patch.object(g.data, "spot", return_value=23500.0), \
                patch.object(g.data, "chain_rows", return_value=CHAIN), \
                patch.object(g.data, "futures_price", return_value=None):
            self.assertIsNone(g.compute_metrics("NIFTY", now)["breakout"])
            g.data.spot_candles["NIFTY"].append({"t": t0 + 9 * 300, "open": 23410.0, "high": 23430.0,
                                                 "low": 23400.0, "close": 23420.0})   # 10:00-10:05 arrives
            self.assertEqual(g.compute_metrics("NIFTY", now)["breakout"], "CALL")

    async def test_expiry_block_only_on_expiry_day_inside_window(self):
        g = self.group
        exp = date(2026, 11, 17)
        self.assertTrue(g.expiry_blocked(exp, datetime(2026, 11, 17, 10, 45)))
        self.assertTrue(g.expiry_blocked(exp, datetime(2026, 11, 17, 11, 44)))
        self.assertFalse(g.expiry_blocked(exp, datetime(2026, 11, 17, 11, 45)))
        self.assertFalse(g.expiry_blocked(exp, datetime(2026, 11, 16, 11, 0)))
        self.assertFalse(g.expiry_blocked(None, datetime(2026, 11, 17, 11, 0)))

    async def test_atm_fallback_when_itm_unaffordable(self):
        eng = self.engine
        eng.enabled = True
        leg = DayLeg("NIFTY", status="watching", or_high=23515.5, or_low=23499.5, strike=23250.0, security_id=1,
                     spot=23520.0, expiry="2026-11-17", prepared_side="CALL")
        leg.status, leg.note = "skipped", "insufficient balance: Rs15,000 < 1 lot"
        self.assertTrue(eng.funds_problem(leg))
        rows = [{"strike": s, "ce": {"securityId": int(s), "ltp": 100.0}, "pe": {"securityId": int(s) + 1}}
                for s in (23250.0, 23500.0, 23550.0)]
        entered = []

        async def fake_enter(l, d):
            entered.append((l.strike, l.security_id, l.status, l.funds_retries))
        with patch.object(algo_module.chain_engine, "snapshot", return_value={"rows": rows}), \
                patch.object(self.group.data, "spot", return_value=23520.0), \
                patch.object(engine_module.feed, "price", return_value=100.0), \
                patch.object(engine_module.feed, "age", return_value=0.1), \
                patch.object(eng, "_enter", side_effect=fake_enter), patch.object(eng, "_log"):
            self.assertTrue(await eng.enter_atm(leg, "CALL", leg.note))
            self.assertFalse(await eng.enter_atm(leg, "CALL", leg.note))   # only one ATM try per signal
        self.assertEqual(entered, [(23500.0, 23500, "watching", 1)])
        broker = DayLeg("NIFTY", status="watching", error="DH-906: Insufficient Funds")
        self.assertTrue(eng.funds_problem(broker))
        self.assertIsNone(eng.funds_problem(DayLeg("NIFTY", status="skipped", note="Contract already held")))

    async def test_dispatch_falls_back_to_atm_after_funds_skip(self):
        g, eng = self.group, self.engine
        eng.enabled = True
        g.entry_accounts = ["master"]
        m = self.v5_metrics()

        async def prep(leg, side):
            leg.strike, leg.security_id, leg.prepared_side = 23250.0, 1, "CALL"

        async def itm_skip(leg, side):
            leg.status, leg.note = "skipped", "insufficient balance: Rs9,000"
        with patch.object(eng, "_prepare_contract", side_effect=prep), \
                patch.object(eng, "_enter", side_effect=itm_skip), \
                patch.object(eng, "free_margin_from_algo1", new_callable=AsyncMock, return_value=False), \
                patch.object(eng, "enter_atm", new_callable=AsyncMock, return_value=True) as atm, \
                patch.object(engine_module.feed, "price", return_value=100.0), \
                patch.object(eng, "_log"):
            await g._dispatch("NIFTY", "CALL", m)
        atm.assert_awaited_once()
        self.assertTrue(g.last_signal["consumed"])
        self.assertEqual(g.signal_ctx["NIFTY"]["candleLow"], 23480.0)   # SL = 15-min candle, not the 5-min one

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
        self.assertEqual(first[2:], ("INDEX", "1", "2026-11-10 09:14:00", "2026-11-10 15:30:00"))
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


class MicrostructureRecorderTests(unittest.TestCase):
    @staticmethod
    def full_packet(sid, ltp):
        head = micro.FULL.pack(8, 162, 2, sid, ltp, 75, 1763000000, ltp - 1, 12345, 900, 1100, 5000, 5100, 4900,
                               ltp - 5, 0.0, ltp + 5, ltp - 10)
        levels = b"".join(micro.LEVEL5.pack(100 + n, 200 + n, 3, 4, ltp - n, ltp + n) for n in range(5))
        return head + levels

    @staticmethod
    def depth_packet(code, sid, base):
        body = b"".join(micro.LEVEL20.pack(base + n, 10 * n, n) for n in range(20))
        return micro.DEPTH_HDR.pack(12 + len(body), code, 2, sid, 7) + body

    def test_recorded_feed_round_trips_raw_bytes_and_decodes(self):
        msg = self.full_packet(111, 250.5) + self.full_packet(222, 99.25)
        with tempfile.TemporaryDirectory() as tmp:
            writer = micro.StreamWriter(os.path.join(tmp, "d", "feed.bin.gz"))
            writer.add(1.5, msg)
            writer.write(writer.take())
            writer.add(2.5, msg[:162])           # second flush -> second gzip member
            writer.write(writer.take())
            records = list(micro.read_records(writer.path))
        self.assertEqual(records, [(1.5, msg), (2.5, msg[:162])])
        packets = micro.split_feed(records[0][1])
        self.assertEqual([len(p) for p in packets], [162, 162])
        first = micro.parse_full(packets[0])
        self.assertEqual((first["securityId"], first["ltq"], first["volume"]), (111, 75, 12345))
        self.assertEqual((first["totalBuyQty"], first["totalSellQty"], first["oi"]), (1100, 900, 5000))
        self.assertAlmostEqual(first["ltp"], 250.5)
        self.assertEqual(first["depth"][4]["bidQty"], 104)
        self.assertAlmostEqual(first["depth"][4]["ask"], 254.5)

    def test_depth20_message_splits_into_bid_and_ask_books(self):
        msg = self.depth_packet(41, 333, 100.0) + self.depth_packet(51, 333, 101.0)
        books = [micro.parse_depth20(p) for p in micro.split_depth(msg)]
        self.assertEqual([b["side"] for b in books], ["bid", "ask"])
        self.assertEqual(len(books[0]["levels"]), 20)
        self.assertEqual(books[1]["levels"][3], {"price": 104.0, "qty": 30, "orders": 3})

    def test_targets_cover_futures_and_atm_options_and_depth_is_nse_only(self):
        rows = [chain_row(k, 1, 1, 0.5, -0.5) for k in (23400, 23450, 23500, 23550, 23600, 23650)]
        snaps = {"NIFTY": {"expiry": "2026-11-17", "underlyingLtp": 23510.0, "rows": rows},
                 "SENSEX": {"expiry": "2026-11-20", "underlyingLtp": 77000.0,
                            "rows": [chain_row(77000, 1, 1, 0.5, -0.5)]}}
        source = MagicMock(futures={"NIFTY": {"securityId": 5001, "segment": "NSE_FNO", "expiry": "2026-11-25"},
                                    "SENSEX": {"securityId": 6001, "segment": "BSE_FNO", "expiry": "2026-11-27"}})
        with tempfile.TemporaryDirectory() as tmp, patch.object(micro.chain_engine, "snapshot", snaps.get):
            rec = micro.MicrostructureRecorder(tmp, source)
            rec.roll_day(datetime(2026, 11, 16, 9, 30))
            t = rec.targets()
            feed_ids = {r["securityId"] for r in t["feed"]}
            self.assertTrue({13, 51, 5001, 6001} <= feed_ids)
            nifty_strikes = {r["strike"] for r in t["feed"] if r["index"] == "NIFTY" and r["kind"] == "option"}
            self.assertEqual(nifty_strikes, {23400, 23450, 23500, 23550, 23600})
            self.assertEqual({r["segment"] for r in t["depth20"]}, {"NSE_FNO"})
            self.assertEqual({r.get("strike") for r in t["depth20"]}, {None, 23450, 23500, 23550})
            self.assertEqual(len(rec.new_targets("feed", t["feed"])), len(t["feed"]))
            self.assertEqual(rec.new_targets("feed", t["feed"]), [])          # deduped
            with open(os.path.join(tmp, "2026-11-16", "instruments.jsonl"), encoding="utf-8") as fh:
                self.assertEqual(sum(1 for _ in fh), len(t["feed"]))
            msg = json.loads(rec.subscribe_messages("feed", t["feed"][:2])[0])
            self.assertEqual((msg["RequestCode"], msg["InstrumentCount"]), (21, 2))

    def test_low_disk_pauses_recording_without_deleting_and_resumes(self):
        with tempfile.TemporaryDirectory() as tmp:
            rec = micro.MicrostructureRecorder(tmp)
            rec.roll_day(datetime(2026, 11, 16, 10, 0))
            rec.record("feed", b"x" * 20, at=1.0)
            rec.flush_all(force=True)
            low = MagicMock(free=micro.MIN_FREE_BYTES - 1)
            with patch.object(micro.shutil, "disk_usage", return_value=low):
                self.assertFalse(rec.disk_ok())
            rec.record("feed", b"y" * 20, at=2.0)                   # dropped while paused
            rec.flush_all(force=True)
            self.assertEqual(list(micro.read_records(rec.writers["feed"].path)), [(1.0, b"x" * 20)])
            with patch.object(micro.shutil, "disk_usage", return_value=MagicMock(free=micro.MIN_FREE_BYTES)):
                self.assertTrue(rec.disk_ok())
            with open(os.path.join(tmp, "2026-11-16", "events.jsonl"), encoding="utf-8") as fh:
                self.assertEqual([json.loads(x)["event"] for x in fh], ["disk_pause", "disk_resume"])

    def test_window_is_weekday_0910_to_1535(self):
        self.assertFalse(micro.in_window(datetime(2026, 11, 16, 9, 9)))
        self.assertTrue(micro.in_window(datetime(2026, 11, 16, 9, 10)))
        self.assertFalse(micro.in_window(datetime(2026, 11, 16, 15, 35)))
        self.assertFalse(micro.in_window(datetime(2026, 11, 15, 11, 0)))   # Sunday


if __name__ == "__main__":
    unittest.main()
