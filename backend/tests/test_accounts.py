"""Offline ASGI -> router -> execution -> broker-transport integration tests."""
import asyncio
import importlib
import json
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx

from app import accounts, audit, algo_store, runtime
from app.accounts import account_scope
from app.config import Settings
from app.dhan_client import DhanClient

algo_module = importlib.import_module("app.algo")
client_module = importlib.import_module("app.dhan_client")


class Broker:
    def __init__(self, identity, balance):
        self.identity = identity
        self.balance = balance
        self.orders = []
        self.supers = []
        self.net = 0
        self.product = "INTRADAY"
        self.reject = False
        self.timeout = False
        self.reject_status = False
        self.ip_allowed = True
        self.ip_response = None
        self.calls = []

    def handle(self, request):
        path = request.url.path.removeprefix("/v2")
        body = json.loads(request.content) if request.content else {}
        self.calls.append((request.method, path, body))
        assert request.headers["client-id"] == self.identity
        assert request.headers["access-token"] == f"token-{self.identity}"
        if request.method == "POST" and path in {"/orders", "/super/orders"}:
            assert body["dhanClientId"] == self.identity
            if self.timeout:
                raise httpx.ReadTimeout("Uncertain broker response", request=request)
            if self.reject:
                return httpx.Response(400, json={"errorCode": "DH-906", "errorMessage": f"{self.identity} insufficient margin"})
            if self.reject_status:
                return httpx.Response(200, json={"orderId": "same-rejected-id",
                    "orderStatus": "REJECTED", "omsErrorDescription": "Risk validation rejected entry"})
            pending = body.get("orderType") == "STOP_LOSS_MARKET"
            order = dict(body, orderId=f"same-id-{len(self.orders) + len(self.supers)}",
                         orderStatus="PENDING" if pending else "TRADED",
                         filledQty=0 if pending else body["quantity"],
                         averageTradedPrice=100, quantity=body["quantity"])
            if not pending:
                self.net += body["quantity"] * (1 if body["transactionType"] == "BUY" else -1)
            if path == "/super/orders":
                order["legDetails"] = [
                    {"legName": "TARGET_LEG", "orderStatus": "PENDING", "price": body["targetPrice"]},
                    {"legName": "STOP_LOSS_LEG", "orderStatus": "PENDING", "price": body["stopLossPrice"]},
                ]
                self.supers.append(order)
            else:
                self.orders.append(order)
            return httpx.Response(200, json={"orderId": order["orderId"], "orderStatus": order["orderStatus"]})
        if request.method == "DELETE":
            if path.startswith("/super/orders/"):
                order_id, leg = path.split("/")[-2:]
                for o in self.supers:
                    if o["orderId"] == order_id:
                        for l in o["legDetails"]:
                            if l["legName"] == leg:
                                l["orderStatus"] = "CANCELLED"
            else:
                for o in self.orders:
                    if o["orderId"] == path.split("/")[-1]:
                        o["orderStatus"] = "CANCELLED"
            return httpx.Response(200, json={"orderStatus": "CANCELLED"})
        if path == "/positions":
            return httpx.Response(200, json=[dict(
                securityId="47153", exchangeSegment="NSE_FNO", productType=self.product,
                netQty=self.net, buyAvg=100, sellAvg=100,
                realizedProfit=0, unrealizedProfit=0,
            )])
        if path == "/fundlimit":
            return httpx.Response(200, json={"availabelBalance": self.balance})
        if path == "/orders":
            return httpx.Response(200, json=self.orders)
        if path == "/super/orders":
            return httpx.Response(200, json=self.supers)
        if path.startswith("/orders/"):
            identity = path.split("/")[-1]
            order = next((o for o in self.orders if o["orderId"] == identity
                          or o.get("correlationId") == identity), [])
            return httpx.Response(200, json=order)
        if path in {"/profile", "/ip/getIP"}:
            if path == "/ip/getIP" and self.ip_response is not None:
                return httpx.Response(200, json=self.ip_response)
            return httpx.Response(200, json={"ordersAllowed": self.ip_allowed,
                "ipMatchStatus": "PRIMARY_MATCH" if self.ip_allowed else "MISMATCH",
                "detectedIP": "192.0.2.1"})
        if path == "/ip/setIP":
            assert body["dhanClientId"] == self.identity
            return httpx.Response(200, json={"status": "SUCCESS"})
        raise AssertionError(f"Unexpected trading endpoint {self.identity}: {request.method} {path}")


class MultiAccountIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        settings = Settings(
            _env_file=None, dhan_client_id="master-id", dhan_access_token="data-token",
            dhan_master_trading_token="token-master-id",
            dhan_child_client_id="child-id", dhan_child_access_token="token-child-id",
            app_dry_run=False, app_fast_orders=True,
        )
        self.patch("app.config.get_settings", return_value=settings)
        self.patch("app.dhan_client.get_settings", return_value=settings)
        self.patch.object(algo_module, "get_settings", return_value=settings)
        self.patch.object(audit, "DB_PATH", root / "audit.sqlite3")
        self.patch.object(algo_store, "_master_store", algo_store.AlgoStore(str(root / "master")))
        self.patch.object(algo_store, "_child_store", algo_store.AlgoStore(str(root / "child")))
        self.patch.object(accounts, "_selected", ("master", "child"))
        self.patch.object(runtime, "_dry_run", False)
        self.patch.object(runtime, "_fast_orders", True)
        self.manager = algo_module.AccountEngines()
        self.manager.entry_accounts = ("master", "child")
        self.patch.object(algo_module.engines, "items", self.manager.items)
        self.patch.object(algo_module.engines, "entry_accounts", ("master", "child"))
        self.patch.object(algo_module.engines, "selection_lock", asyncio.Lock())
        for account, engine in self.manager.items.items():
            engine._day = algo_module.now_ist().date().isoformat()
            # Fixtures below model INTRADAY books; the MARGIN default has its own test.
            engine.cfg.product_type = "INTRADAY"
            with account_scope(account):
                audit.init_db()
        self.brokers = {"master": Broker("master-id", 100000),
                        "child": Broker("child-id", 300000)}
        clients = {}
        for account, broker in self.brokers.items():
            c = DhanClient(client_id=broker.identity,
                           access_token=f"token-{broker.identity}", account_id=account)
            await c._client.aclose()
            c._client = httpx.AsyncClient(base_url="https://api.dhan.co/v2",
                                          transport=httpx.MockTransport(broker.handle))
            clients[account] = c
            self.addAsyncCleanup(c.aclose)
        self.patch.object(client_module, "_trading_clients", clients)
        self.patch.object(client_module, "gate_for", return_value=MagicMock(acquire=AsyncMock()))
        self.data_calls = []
        data = MagicMock()
        async def quotes(instruments):
            self.data_calls.append(instruments)
            return {"data": {seg: {str(sid): {"last_price": 100} for sid in ids}
                             for seg, ids in instruments.items()}}
        data.get_ltp = AsyncMock(side_effect=quotes)
        self.patch("app.feed.get_dhan_client", return_value=data)
        self.patch("app.logsetup.setup_logging")
        from app.main import app
        from app.routers import account as account_routes
        self.patch.object(account_routes, "_pending_exits", {})
        self.patch.object(account_routes, "_child_pending_exits", {})
        from app.security import require_csrf, require_session
        self.app = app
        self.app.dependency_overrides[require_session] = lambda: "test"
        self.app.dependency_overrides[require_csrf] = lambda: None
        self.addCleanup(self.app.dependency_overrides.clear)
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test")
        self.addAsyncCleanup(self.http.aclose)
        self.patch.object(socket.socket, "connect", side_effect=AssertionError("Live network forbidden"))
        self.body = dict(indexKey="NIFTY", securityId="47153", optionType="CE",
                         transactionType="BUY", quantity=1, price=100, targetPct=5,
                         pin="", requestId=str(uuid4()), accountIds=["master", "child"])

    # Keep patch.object syntax terse without bypassing teardown.
    @property
    def patch(self):
        owner = self
        class Patcher:
            def __call__(self, target, **kwargs):
                p = patch(target, **kwargs)
                value = p.start()
                owner.addCleanup(p.stop)
                return value
            def object(self, obj, name, *args, **kwargs):
                p = patch.object(obj, name, *args, **kwargs)
                value = p.start()
                owner.addCleanup(p.stop)
                return value
        return Patcher()

    async def test_account_status_distinguishes_mock_data_and_each_broker(self):
        from app.routers import trading_accounts

        mixed = Settings(
            _env_file=None, dhan_client_id="master-id", dhan_access_token="Mock-data",
            dhan_master_trading_token="real-master",
            dhan_child_client_id="child-id", dhan_child_access_token="MOCK-child",
        )
        self.patch("app.config.get_settings", return_value=mixed)
        self.patch.object(trading_accounts, "get_settings", return_value=mixed)
        response = await self.http.get("/api/trading/accounts")
        self.assertEqual(response.status_code, 200, response.text)
        status = response.json()
        self.assertTrue(status["dataSourceMock"])
        accounts_by_id = {account["id"]: account for account in status["accounts"]}
        self.assertFalse(accounts_by_id["master"]["mock"])
        self.assertTrue(accounts_by_id["child"]["mock"])
        self.assertNotIn("real-master", response.text)
        self.assertNotIn("MOCK-child", response.text)

    async def test_account_status_distinguishes_live_data_and_unconfigured_account(self):
        from app.routers import trading_accounts

        mixed = Settings(
            _env_file=None, dhan_client_id="master-id", dhan_access_token="live-data",
            dhan_master_trading_token="mock-master",
        )
        self.patch("app.config.get_settings", return_value=mixed)
        self.patch.object(trading_accounts, "get_settings", return_value=mixed)
        response = await self.http.get("/api/trading/accounts")
        self.assertEqual(response.status_code, 200, response.text)
        status = response.json()
        self.assertFalse(status["dataSourceMock"])
        accounts_by_id = {account["id"]: account for account in status["accounts"]}
        self.assertTrue(accounts_by_id["master"]["mock"])
        self.assertFalse(accounts_by_id["child"]["configured"])
        self.assertFalse(accounts_by_id["child"]["mock"])

    async def test_manual_both_entry_account_reads_and_exit_both(self):
        r = await self.http.post("/api/trading/place", json=self.body)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["ok"])
        self.assertEqual([b.net for b in self.brokers.values()], [65, 65])
        self.assertEqual([len(b.supers) for b in self.brokers.values()], [1, 1])
        for account in ("master", "child"):
            rows = (await self.http.get("/api/account/positions",
                headers={"X-Trading-Account": account})).json()
            self.assertEqual(rows[0]["netQty"], 65)
            self.assertEqual(rows[0]["ltp"], 100)
        result = await self.http.post("/api/trading/exit-both", json={
            "securityId": "47153", "exchangeSegment": "NSE_FNO", "productType": "INTRADAY",
        })
        self.assertTrue(result.json()["ok"], result.text)
        self.assertEqual([b.net for b in self.brokers.values()], [0, 0])
        for b in self.brokers.values():
            self.assertTrue(all(l["orderStatus"] == "CANCELLED" for l in b.supers[0]["legDetails"]))

    async def test_partial_account_failure_keeps_success_and_explains_failure(self):
        self.brokers["child"].reject = True
        response = await self.http.post("/api/trading/place", json=self.body)
        result = response.json()
        self.assertFalse(result["ok"])
        self.assertTrue(result["results"][0]["ok"])
        self.assertFalse(result["results"][1]["ok"])
        self.assertIn("insufficient margin", str(result["results"][1]["error"]))
        self.assertEqual(self.brokers["master"].net, 65)
        self.assertEqual(self.brokers["child"].net, 0)

    async def test_timeout_retry_and_concurrent_doubleclick_never_repeat_entries(self):
        self.brokers["child"].timeout = True
        first, second = await asyncio.gather(
            self.http.post("/api/trading/place", json=self.body),
            self.http.post("/api/trading/place", json=self.body),
        )
        self.assertEqual(first.json(), second.json())
        self.assertEqual(len(self.brokers["master"].supers), 1)
        self.assertEqual(sum(c[0] == "POST" for c in self.brokers["child"].calls), 1)

    async def test_recipient_change_after_submission_cannot_add_new_order_on_retry(self):
        await self.http.post("/api/trading/place", json=self.body)
        self.patch.object(accounts, "_selected", ("child",))
        self.body["accountIds"] = ["child"]
        response = await self.http.post("/api/trading/place", json=self.body)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(len(self.brokers["child"].supers), 1)

    async def test_master_only_child_only_and_no_recipient(self):
        for account in ("master", "child"):
            accounts.select_accounts([account])
            body = {**self.body, "accountIds": [account], "requestId": str(uuid4())}
            result = await self.http.post("/api/trading/place", json=body)
            self.assertTrue(result.json()["ok"])
            self.assertEqual([r["accountId"] for r in result.json()["results"]], [account])
        accounts.select_accounts([])
        r = await self.http.post("/api/trading/place", json=self.body)
        self.assertEqual(r.status_code, 409)

    async def test_single_account_exit_never_touches_other_account(self):
        await self.http.post("/api/trading/place", json=self.body)
        result = await self.http.post("/api/account/squareoff",
            headers={"X-Trading-Account": "child"}, json={
                "securityId": "47153", "exchangeSegment": "NSE_FNO",
                "productType": "INTRADAY", "quantity": 65, "transactionType": "SELL",
            })
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(self.brokers["master"].net, 65)
        self.assertEqual(self.brokers["child"].net, 0)

    async def test_preview_uses_independent_funds_and_reports_per_account(self):
        self.brokers["child"].balance = 100
        result = (await self.http.post("/api/trading/preview", json=self.body)).json()
        self.assertTrue(result["results"][0]["result"]["funds"]["sufficient"])
        self.assertFalse(result["results"][1]["result"]["funds"]["sufficient"])
        placed = (await self.http.post("/api/trading/place", json=self.body)).json()
        self.assertTrue(placed["results"][0]["ok"])
        self.assertFalse(placed["results"][1]["ok"])

    async def test_dry_run_validates_both_but_never_sends_orders(self):
        runtime._dry_run = True
        result = (await self.http.post("/api/trading/place", json=self.body)).json()
        self.assertTrue(result["ok"])
        self.assertTrue(all(r["result"]["dryRun"] for r in result["results"]))
        self.assertTrue(all(not b.supers and not b.orders for b in self.brokers.values()))

    async def test_unknown_account_rejected_and_selected_config_not_exposing_secrets(self):
        r = await self.http.get("/api/account/positions", headers={"X-Trading-Account": "other"})
        self.assertEqual(r.status_code, 400)
        r = await self.http.get("/api/trading/accounts")
        self.assertNotIn("token-", r.text)
        self.assertNotIn("data-token", r.text)
        self.assertEqual(r.json()["dataSource"], "master")

    async def test_audit_intents_exit_keys_and_state_are_isolated_even_same_ids(self):
        for account in ("master", "child"):
            with account_scope(account):
                self.assertTrue(audit.claim_manual_request("same-uuid", account)[0])
                audit.save_manual_exit(("47153", "NSE_FNO", "INTRADAY"), account)
                algo_store.store.save_state({"account": account})
        for account in ("master", "child"):
            with account_scope(account):
                self.assertEqual(audit.pending_manual_exits()[("47153", "NSE_FNO", "INTRADAY")], account)
                self.assertEqual(algo_store.store.load_state(), {"account": account})
                self.assertFalse(audit.claim_manual_request("same-uuid", account)[0])

    async def dispatch_shared_signal(self):
        source = algo_module.DayLeg("NIFTY", status="watching",
            or_high=23000, or_low=22900, spot=23001)
        for e in self.manager.items.values():
            e.cfg.risk_pct = 10
            e.cfg.max_capital_pct = 60
            e.cfg.protective_sl = True
            e.cfg.indices = ["NIFTY"]
        snap = {"expiry": "2026-10-29", "underlyingLtp": 23001, "rows": [{
            "strike": 23050, "ce": {"securityId": 47153, "ltp": 100},
            "pe": {"securityId": 47154, "ltp": 100},
        }]}
        with patch.object(algo_module.chain_engine, "snapshot", return_value=snap), \
                patch.object(algo_module.feed, "price", return_value=100), \
                patch.object(algo_module.feed, "age", return_value=0), \
                patch.object(algo_module.feed, "subscribe"):
            await self.manager.enter_signal(source, "CALL")

    async def test_shared_algo_signal_same_direction_independent_sizing_and_protective_orders(self):
        await self.dispatch_shared_signal()
        master, child = [e._position for e in self.manager.items.values()]
        self.assertIsNotNone(master)
        self.assertIsNotNone(child)
        self.assertEqual(master.option_type, child.option_type)
        self.assertEqual(master.security_id, child.security_id)
        self.assertEqual(master.target_spot, child.target_spot)
        self.assertNotEqual(master.quantity, child.quantity)
        for account, engine in self.manager.items.items():
            self.assertEqual(engine._position.execution_state, "open")
            self.assertIsNotNone(engine._position.protective_order_id)
            with account_scope(account):
                await engine.panic_exit("test")
                engine._position.entry_confirmed_ts -= 4
                await engine.reconcile()
            self.assertIsNone(engine._position)
        self.assertEqual([b.net for b in self.brokers.values()], [0, 0])

    async def test_algo_child_rejection_keeps_master_fill_and_protection(self):
        self.brokers["child"].reject = True
        await self.dispatch_shared_signal()
        master, child = [e._position for e in self.manager.items.values()]
        self.assertEqual(master.execution_state, "open")
        self.assertIsNotNone(master.protective_order_id)
        self.assertIsNone(child)
        self.assertIn("insufficient margin", self.manager.items["child"]._last_error)
        self.assertFalse(any(o["transactionType"] == "SELL" and o["orderType"] == "MARKET"
                             for o in self.brokers["master"].orders))

    async def test_algo_child_timeout_tracks_unknown_without_repeating_buy(self):
        self.brokers["child"].timeout = True
        await self.dispatch_shared_signal()
        master, child = [e._position for e in self.manager.items.values()]
        self.assertEqual(master.execution_state, "open")
        self.assertEqual(child.execution_state, "entry_pending")
        self.assertIn("unknown", child.execution_error)
        self.assertTrue(child.entry_correlation_id)
        self.assertEqual(sum(method == "POST" for method, _, _ in self.brokers["child"].calls), 1)

    async def test_selection_allowed_with_position_but_mode_change_blocked(self):
        from tests.test_trading import position
        self.manager.items["child"]._position = position()
        r = await self.http.post("/api/trading/selection", json={"accounts": ["master"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNotNone(self.manager.items["child"]._position)  # exit management untouched
        runtime._dry_run = True
        r = await self.http.post("/api/settings/dry-run", json={"value": False})
        self.assertEqual(r.status_code, 409)
        self.assertTrue(runtime.is_dry_run())

    def _fake_preflight(self, ip_ok):
        for account, engine in self.manager.items.items():
            async def preflight(account=account):
                return {"ok": ip_ok[account], "reason": f"{account} ip not whitelisted"}
            self.patch.object(engine, "_preflight_ip", new=preflight)

    async def test_ticks_change_live_while_armed(self):
        ip_ok = {"master": True, "child": True}
        self._fake_preflight(ip_ok)
        for engine in self.manager.items.values():
            engine.enabled = True
        master, child = self.manager.items["master"], self.manager.items["child"]
        # Untick child: child stops new entries, master keeps running.
        r = await self.http.post("/api/trading/selection", json={"accounts": ["master"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(algo_module.engines.entry_accounts, ("master",))
        self.assertTrue(master.enabled)
        self.assertFalse(child.enabled)
        # Untick master, tick child: master still drives the signal but places no entry.
        r = await self.http.post("/api/trading/selection", json={"accounts": ["child"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(algo_module.engines.entry_accounts, ("child",))
        self.assertTrue(master.enabled and child.enabled)
        # A failing IP check refuses the add and leaves everything as it was.
        ip_ok["master"] = False
        r = await self.http.post("/api/trading/selection", json={"accounts": ["master", "child"]})
        self.assertEqual(r.status_code, 409)
        self.assertIn("not whitelisted", r.text)
        self.assertEqual(accounts.selected_accounts(), ("child",))
        self.assertEqual(algo_module.engines.entry_accounts, ("child",))
        ip_ok["master"] = True
        r = await self.http.post("/api/trading/selection", json={"accounts": ["master", "child"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(algo_module.engines.entry_accounts, ("master", "child"))
        # Untick everything = algo off for new entries.
        r = await self.http.post("/api/trading/selection", json={"accounts": []})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(any(e.enabled for e in self.manager.items.values()))

    async def test_exit_rules_save_only_to_ticked_accounts_and_survive_arm(self):
        self._fake_preflight({"master": True, "child": True})
        master, child = self.manager.items["master"], self.manager.items["child"]
        accounts.select_accounts(["master"])
        r = await self.http.post("/api/algo/exit-rules", json={"profit_target_rs": 1000, "stop_loss_rs": 500})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["savedFor"], ["master"])
        self.assertEqual((master.cfg.profit_target_rs, master.cfg.stop_loss_rs), (1000, 500))
        self.assertEqual((child.cfg.profit_target_rs, child.cfg.stop_loss_rs), (0, 0))
        r = await self.http.post("/api/algo/exit-rules", headers={"X-Trading-Account": "child"},
                                 json={"profit_target_rs": 700, "stop_loss_rs": 300, "accounts": ["child"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["exitRules"]["child"], {"profit_target_rs": 700, "stop_loss_rs": 300})
        self.assertEqual(r.json()["exitRules"]["master"], {"profit_target_rs": 1000, "stop_loss_rs": 500})
        # Shared ARM copies strategy config to child but keeps child's own rupee exits.
        accounts.select_accounts(["master", "child"])
        for engine in self.manager.items.values():
            engine.enabled = False
        r = await self.http.post("/api/algo/arm")
        self.assertTrue(r.json()["ok"], r.text)
        self.assertEqual((child.cfg.profit_target_rs, child.cfg.stop_loss_rs), (700, 300))
        self.assertEqual((master.cfg.profit_target_rs, master.cfg.stop_loss_rs), (1000, 500))
        status = (await self.http.get("/api/algo/status")).json()
        self.assertEqual(status["exitRules"]["child"]["profit_target_rs"], 700)
        r = await self.http.post("/api/algo/exit-rules", json={"profit_target_rs": 1, "stop_loss_rs": 1, "accounts": []})
        self.assertEqual(r.status_code, 400)

    async def test_data_quotes_do_not_touch_child_data_endpoints(self):
        await self.http.get("/api/account/positions", headers={"X-Trading-Account": "child"})
        self.assertEqual(len(self.data_calls), 1)
        self.assertFalse(any("/marketfeed" in path or "/charts" in path or "/optionchain" in path
            for _, path, _ in self.brokers["child"].calls))

    async def test_algo_child_only_arms_coordinator_but_never_places_master_entry(self):
        accounts.select_accounts(["child"])
        r = await self.http.post("/api/algo/arm")
        self.assertTrue(r.json()["ok"], r.text)
        self.assertTrue(self.manager.items["master"].enabled)
        self.assertTrue(self.manager.items["child"].enabled)
        self.assertEqual(self.manager.items["master"].cfg, self.manager.items["child"].cfg)
        self.manager.entry_accounts = ("child",)
        source = algo_module.DayLeg("NIFTY", or_high=23000, or_low=22900, spot=23001,
                                   security_id=47154, prepared_side="PUT")
        for engine in self.manager.items.values():
            engine._enter = AsyncMock()
        with patch.object(algo_module.feed, "price", return_value=100), \
                patch.object(algo_module.feed, "age", return_value=0):
            await self.manager.enter_signal(source, "PUT")
        self.manager.items["master"]._enter.assert_not_awaited()
        self.manager.items["child"]._enter.assert_awaited_once()
        await self.http.post("/api/algo/disarm")
        self.assertTrue(all(not e.enabled for e in self.manager.items.values()))

    async def test_signal_partial_failure_retains_other_account_and_saves_no_replay_day(self):
        async def fail(*args):
            raise OSError("Test child persistence unavailable")
        self.manager.items["master"]._enter = AsyncMock()
        self.manager.items["child"]._enter = AsyncMock(side_effect=fail)
        source = algo_module.DayLeg("NIFTY", or_high=23000, or_low=22900, spot=23001,
                                   security_id=47153, prepared_side="CALL")
        with patch.object(algo_module.feed, "price", return_value=100), \
                patch.object(algo_module.feed, "age", return_value=0):
            await self.manager.enter_signal(source, "CALL")
        self.manager.items["master"]._enter.assert_awaited_once()
        self.assertIn("persistence unavailable", self.manager.items["child"]._last_error)
        with account_scope("master"):
            saved = algo_store.store.load_state()
            self.assertEqual(saved["signalDay"], algo_module.now_ist().date().isoformat())

    async def test_stale_premium_at_breakout_retries_instead_of_consuming_the_day(self):
        # 2026-11-03: the contract was subscribed on the breakout tick, its premium was
        # not fresh yet, and the shared signal locked the day without any order.
        source = algo_module.DayLeg("NIFTY", status="watching",
            or_high=23000, or_low=22900, spot=23001)
        for e in self.manager.items.values():
            e.cfg.risk_pct = 10
            e.cfg.max_capital_pct = 60
            e.cfg.protective_sl = True
            e.cfg.indices = ["NIFTY"]
        snap = {"expiry": "2026-10-29", "underlyingLtp": 23001, "rows": [{
            "strike": 23050, "ce": {"securityId": 47153, "ltp": 100},
            "pe": {"securityId": 47154, "ltp": 100},
        }]}
        with patch.object(algo_module.chain_engine, "snapshot", return_value=snap), \
                patch.object(algo_module.feed, "subscribe"), \
                patch.object(algo_module.feed, "price", return_value=None), \
                patch.object(algo_module.feed, "age", return_value=None):
            await self.manager.enter_signal(source, "CALL")
        master = self.manager.items["master"]
        self.assertIsNone(master._signal_day)
        self.assertEqual(source.status, "watching")
        self.assertIn("fresh option premium", source.note)
        self.assertTrue(all(not b.orders and not b.supers for b in self.brokers.values()))
        with patch.object(algo_module.chain_engine, "snapshot", return_value=snap), \
                patch.object(algo_module.feed, "subscribe"), \
                patch.object(algo_module.feed, "price", return_value=100), \
                patch.object(algo_module.feed, "age", return_value=0):
            await self.manager.enter_signal(source, "CALL")
        self.assertTrue(all(e._position is not None for e in self.manager.items.values()))
        self.assertEqual(master._signal_day, algo_module.now_ist().date().isoformat())

    async def test_breakout_reverting_before_order_does_not_lock_the_day(self):
        # 2026-11-04: NIFTY crossed the OR high, spot fell back to 23906.15 (< 23906.6)
        # during pre-order checks, both accounts aborted, and the day stayed locked.
        source = algo_module.DayLeg("NIFTY", status="watching",
            or_high=23000, or_low=22900, spot=23001)
        for e in self.manager.items.values():
            e.cfg.risk_pct = 10
            e.cfg.max_capital_pct = 60
            e.cfg.protective_sl = True
            e.cfg.indices = ["NIFTY"]
        snap = {"expiry": "2026-10-29", "underlyingLtp": 23001, "rows": [{
            "strike": 23050, "ce": {"securityId": 47153, "ltp": 100},
            "pe": {"securityId": 47154, "ltp": 100},
        }]}
        originals = {a: e._size_position for a, e in self.manager.items.items()}
        def reverting(account):
            async def size(leg, **kwargs):
                leg.spot = 22999.5
                return await originals[account](leg, **kwargs)
            return size
        for account, engine in self.manager.items.items():
            engine._size_position = reverting(account)
        feeds = (patch.object(algo_module.chain_engine, "snapshot", return_value=snap),
                 patch.object(algo_module.feed, "subscribe"),
                 patch.object(algo_module.feed, "price", return_value=100),
                 patch.object(algo_module.feed, "age", return_value=0))
        with feeds[0], feeds[1], feeds[2], feeds[3]:
            await self.manager.enter_signal(source, "CALL")
        master = self.manager.items["master"]
        self.assertIsNone(master._signal_day)
        self.assertTrue(all(e._position is None for e in self.manager.items.values()))
        self.assertTrue(all(not b.orders and not b.supers for b in self.brokers.values()))
        leg = master._legs["NIFTY"]
        self.assertEqual(leg.status, "watching")
        self.assertGreater(leg.next_try_at, time.time())
        # A genuine breakout later in the day must still be taken by both accounts.
        for account, engine in self.manager.items.items():
            engine._size_position = originals[account]
        leg.spot = 23005
        with feeds[0], feeds[1], feeds[2], feeds[3]:
            await self.manager.enter_signal(leg, "CALL")
        self.assertTrue(all(e._position is not None for e in self.manager.items.values()))
        self.assertEqual(master._signal_day, algo_module.now_ist().date().isoformat())

    async def test_end_to_end_auto_arm_entry_target_exit_flat_on_both_accounts(self):
        """Startup auto-arm -> stale-premium retry -> both entries -> target -> SELL -> broker flat."""
        from app.routers import trading_accounts

        auto = Settings(_env_file=None, dhan_client_id="master-id", dhan_access_token="data-token",
                        dhan_master_trading_token="token-master-id",
                        dhan_child_client_id="child-id", dhan_child_access_token="token-child-id",
                        TRADING_DEFAULT_ACCOUNTS="master,child", algo_auto_arm=True)
        self.patch.object(trading_accounts, "get_settings", return_value=auto)
        self.patch.object(accounts, "_selected", ("master",))
        algo_module.engines.entry_accounts = ()
        for e in self.manager.items.values():
            e.enabled = False
            e.cfg.risk_pct = 10
            e.cfg.max_capital_pct = 60
            e.cfg.protective_sl = False
            e.cfg.target_only = True
            e.cfg.indices = ["NIFTY"]
        # 1. Startup defaults arm both accounts through the broker IP preflight.
        armed = await trading_accounts.apply_startup_defaults()
        self.assertTrue(armed["ok"], armed)
        self.assertEqual(algo_module.engines.entry_accounts, ("master", "child"))
        self.assertTrue(any(path == "/ip/getIP" for b in self.brokers.values() for _, path, _ in b.calls))
        # 2. Breakout before the option premium is fresh: no order, day not consumed.
        #    Driven through the real entry gate, exactly as at 09:26 on 2026-11-03.
        source = algo_module.DayLeg("NIFTY", status="watching", or_high=23000, or_low=22900, spot=23001)
        master = self.manager.items["master"]
        master._legs["NIFTY"] = source
        snap = {"expiry": "2026-10-29", "underlyingLtp": 23001, "rows": [{
            "strike": 23050, "ce": {"securityId": 47153, "ltp": 100},
            "pe": {"securityId": 47154, "ltp": 100},
        }]}
        nifty = algo_module.get_index("NIFTY").sec_id
        at_0926 = algo_module.datetime(2026, 11, 3, 9, 26, tzinfo=algo_module.IST)
        with patch.object(algo_module, "now_ist", return_value=at_0926), \
                patch.object(algo_module.chain_engine, "snapshot", return_value=snap), \
                patch.object(algo_module.feed, "subscribe"), \
                patch.object(algo_module.feed, "price", return_value=None), \
                patch.object(algo_module.feed, "age",
                             side_effect=lambda sid, seg=None: 0 if sid == nifty else None), \
                account_scope("master"):
            await master._look_for_entry()
        self.assertTrue(all(not b.orders and not b.supers for b in self.brokers.values()))
        # 3. Next fresh tick: real BUY orders reach both brokers.
        with patch.object(algo_module, "now_ist", return_value=at_0926), \
                patch.object(algo_module.chain_engine, "snapshot", return_value=snap), \
                patch.object(algo_module.feed, "subscribe"), \
                patch.object(algo_module.feed, "price", return_value=100), \
                patch.object(algo_module.feed, "age", return_value=0), \
                account_scope("master"):
            await master._look_for_entry()
        positions = {a: e._position for a, e in self.manager.items.items()}
        self.assertTrue(all(p is not None and not p.dry_run for p in positions.values()), positions)
        self.assertTrue(all(b.net > 0 for b in self.brokers.values()))
        self.assertTrue(all(any(o["transactionType"] == "BUY" for o in b.orders + b.supers)
                            for b in self.brokers.values()))
        # 4. Target reached: each account sends a SELL for exactly its broker holding.
        for account, engine in self.manager.items.items():
            pos = positions[account]
            self.assertEqual(pos.execution_state, "open")
            pos.spot = pos.target_spot + 1
            with account_scope(account), patch.object(algo_module.feed, "age", return_value=0):
                await engine._manage_position()
            self.assertEqual(pos.exit_reason, "TARGET")
            sells = [o for o in self.brokers[account].orders if o["transactionType"] == "SELL"]
            self.assertEqual([o["quantity"] for o in sells], [pos.quantity])
        self.assertEqual([b.net for b in self.brokers.values()], [0, 0])
        # 5. Broker flat -> position closed and journalled; no repeat SELL.
        for account, engine in self.manager.items.items():
            positions[account].entry_confirmed_ts = 0
            with account_scope(account):
                await engine.reconcile()
                await engine.reconcile()
            self.assertIsNone(engine._position)
            self.assertEqual(engine._today_trades[-1]["exitReason"], "TARGET")
            self.assertFalse(engine._today_trades[-1]["dryRun"])
            self.assertEqual(len([o for o in self.brokers[account].orders
                                  if o["transactionType"] == "SELL"]), 1)

    async def test_two_trades_per_day_fresh_breakout_all_flat_margin(self):
        """Trade 1 CALL -> both flat -> no re-buy while spot still above high ->
        back inside -> PUT trade 2 -> day locked. MARGIN (carry-forward) product."""
        master, child = self.manager.items["master"], self.manager.items["child"]
        self.enterContext(patch.dict(algo_module.SPECIAL_SESSIONS, clear=True))
        for b in self.brokers.values():
            b.product = "MARGIN"
        for e in self.manager.items.values():
            e.enabled = True
            e.cfg.product_type = "MARGIN"
            e.cfg.risk_pct = 10
            e.cfg.protective_sl = False
            e.cfg.target_only = True
            e.cfg.indices = ["NIFTY", "SENSEX"]
        master._legs = {
            "NIFTY": algo_module.DayLeg("NIFTY", status="watching", or_high=23000, or_low=22900, spot=23001),
            "SENSEX": algo_module.DayLeg("SENSEX", status="watching", or_high=76000, or_low=75800, spot=75900),
        }
        snap = {"expiry": "2026-10-29", "underlyingLtp": 23000, "rows": [{
            "strike": k, "ce": {"securityId": 47153 if k == 23050 else 47000 + k // 50, "ltp": 100},
            "pe": {"securityId": 48000 + k // 50, "ltp": 100}} for k in range(22800, 23201, 50)]}
        at = algo_module.datetime(2026, 11, 3, 9, 40, tzinfo=algo_module.IST)
        today, minute = at.date().isoformat(), 9 * 60 + 40

        async def look():
            with patch.object(algo_module, "now_ist", return_value=at), \
                    patch.object(algo_module.chain_engine, "snapshot", return_value=snap), \
                    patch.object(algo_module.feed, "subscribe"), \
                    patch.object(algo_module.feed, "price", return_value=100), \
                    patch.object(algo_module.feed, "age", return_value=0), \
                    account_scope("master"):
                await master._look_for_entry()

        def buys():
            return [sum(1 for o in b.orders if o["transactionType"] == "BUY") for b in self.brokers.values()]

        async def exit_flat(account, direction):
            engine = self.manager.items[account]
            pos = engine._position
            pos.spot = pos.target_spot + (1 if direction == "CALL" else -1)
            with account_scope(account), patch.object(algo_module.feed, "age", return_value=0):
                await engine._manage_position()
                pos.entry_confirmed_ts = 0
                await engine.reconcile()
                await engine.reconcile()
            self.assertIsNone(engine._position)

        # Trade 1: NIFTY breaks high -> CALL on both accounts, MARGIN product; SENSEX locked.
        await look()
        self.assertEqual(buys(), [1, 1], {k: (l.status, l.note) for k, l in master._legs.items()})
        self.assertTrue(all(o["productType"] == "MARGIN" for b in self.brokers.values() for o in b.orders))
        self.assertTrue(all(b.net > 0 for b in self.brokers.values()))
        self.assertEqual(master._signals_used(today), 1)
        self.assertNotIn("watching", [l.status for l in master._legs.values()])

        # Master exits first; child still holds -> no re-watch, no new order.
        await exit_flat("master", "CALL")
        master._maybe_open_next_trade(today, minute)
        self.assertNotIn("watching", [l.status for l in master._legs.values()])
        await look()
        self.assertEqual(buys(), [1, 1])

        # Child exits -> every account flat -> both indices re-watched.
        await exit_flat("child", "CALL")
        master._maybe_open_next_trade(today, minute)
        self.assertEqual([l.status for l in master._legs.values()], ["watching", "watching"])

        # Spot still above the OR high: not a fresh breakout -> no re-buy.
        master._legs["NIFTY"].spot = 23010
        await look()
        await look()
        self.assertEqual(buys(), [1, 1])
        # Back inside the range, then breaks the low -> PUT trade 2 on both accounts.
        master._legs["NIFTY"].spot = 22950
        await look()
        self.assertEqual(buys(), [1, 1])
        master._legs["NIFTY"].spot = 22899
        await look()
        self.assertEqual(buys(), [2, 2])
        self.assertEqual({e._position.option_type for e in self.manager.items.values()}, {"PUT"})
        self.assertEqual(master._signals_used(today), 2)

        # After trade 2 nothing re-opens: day is done.
        await exit_flat("master", "PUT")
        await exit_flat("child", "PUT")
        master._maybe_open_next_trade(today, minute)
        self.assertNotIn("watching", [l.status for l in master._legs.values()])
        master._legs["NIFTY"].status = "watching"  # even a stray watching leg must not trade
        master._legs["NIFTY"].spot = 23050
        await look()
        self.assertEqual(buys(), [2, 2])
        self.assertEqual([len(e._today_trades) for e in self.manager.items.values()], [2, 2])

    async def test_max_affordable_sizing_buys_every_lot_the_balance_allows(self):
        """User example: balance 15000, lot 65, premium 100 -> 6500/lot -> 2 lots = 130 qty."""
        engine = self.manager.items["master"]
        engine.cfg.max_affordable = True
        engine.cfg.max_lots = 10
        leg = algo_module.DayLeg("NIFTY", status="watching", or_high=23000, or_low=22900)
        leg.option_ltp = 100
        with patch.object(algo_module, "lot_size_for", return_value=65), \
                patch.object(engine, "_balance", AsyncMock(return_value=15000)):
            sizing = await engine._size_position(leg)
            self.assertEqual((sizing["lots"], sizing["quantity"]), (2, 130))
            # 2026-11-04 child reject: 10% buffer -> only 90% of balance is usable.
            with patch.object(engine, "_balance", AsyncMock(return_value=14000)):
                self.assertEqual((await engine._size_position(leg))["lots"], 1)  # 12600/6500
            self.assertEqual((await engine._size_position(leg, balance=14500))["lots"], 2)
            self.assertIn("90% usable", sizing["reason"])
            leg.option_ltp = 210  # 13650/lot > 90% of 15000 -> cannot afford one lot
            self.assertEqual((await engine._size_position(leg))["lots"], 0)
            leg.option_ltp = 1  # 13500 usable / 65 -> 207 lots: max_lots=10 must NOT cap it
            sizing = await engine._size_position(leg)
            # Only the exchange freeze bounds a single order: NIFTY 1800 -> 27 whole lots.
            self.assertEqual((sizing["lots"], sizing["quantity"]), (27, 1755))
            self.assertIn("freeze", sizing["reason"])
        for key, lot, premium, balance, expected in (
                ("BANKNIFTY", 30, 100, 50000, 15),   # 45000 / 3000 -> 15, under freeze 900 (30 lots)
                ("SENSEX", 20, 100, 50000, 22),      # 45000 / 2000 -> 22, under freeze 1000 (50 lots)
                ("BANKNIFTY", 30, 10, 500000, 30)):  # 1500 affordable, freeze 900 -> 30 lots
            leg = algo_module.DayLeg(key, status="watching", or_high=1, or_low=0)
            leg.option_ltp = premium
            with patch.object(algo_module, "lot_size_for", return_value=lot), \
                    patch.object(engine, "_balance", AsyncMock(return_value=balance)):
                self.assertEqual((await engine._size_position(leg))["lots"], expected, key)

    async def test_strikes_are_decided_from_or_levels_so_warm_contract_is_traded(self):
        # 2026-11-04: strike was re-picked from live spot at breakout, so a contract never
        # warmed before had no fresh premium and entry waited a tick.
        engine = self.manager.items["master"]
        engine.cfg.strike_offset = 1
        rows = [{"strike": k, "ce": {"securityId": 1000 + k // 50, "ltp": 100},
                 "pe": {"securityId": 2000 + k // 50, "ltp": 100}} for k in range(22700, 23401, 50)]
        snap = {"expiry": "2026-10-29", "underlyingLtp": 22950, "rows": rows}
        leg = algo_module.DayLeg("NIFTY", status="watching", or_high=23000, or_low=22900, spot=22950)
        with patch.object(algo_module.chain_engine, "snapshot", return_value=snap), \
                patch.object(algo_module.feed, "subscribe") as sub:
            await engine._prepare_contract(leg)
            warmed = {c.args[0] for c in sub.call_args_list}
            self.assertEqual(warmed, {1000 + 23050 // 50, 2000 + 22850 // 50})
            leg.spot = 23001.5
            await engine._prepare_contract(leg, "CALL")
            self.assertEqual((leg.strike, leg.security_id), (23050, 1000 + 23050 // 50))
            leg.spot = 22898
            await engine._prepare_contract(leg, "PUT")
            self.assertEqual((leg.strike, leg.security_id), (22850, 2000 + 22850 // 50))
            leg.spot = 23200  # far past the level (late arm): fall back to live spot
            await engine._prepare_contract(leg, "CALL")
            self.assertEqual(leg.strike, 23250)

    async def _rupee_exit(self, premium, profit, loss):
        await self.dispatch_shared_signal()
        for engine in self.manager.items.values():
            engine.enabled = True  # rules must be editable while armed and holding
        r = await self.http.post("/api/algo/exit-rules",
                                 json={"profit_target_rs": profit, "stop_loss_rs": loss})
        self.assertEqual(r.status_code, 200, r.text)
        positions = {}
        for account, engine in self.manager.items.items():
            self.assertEqual((engine.cfg.profit_target_rs, engine.cfg.stop_loss_rs), (profit, loss))
            pos = positions[account] = engine._position
            pos.spot = None  # rupee exits must not depend on the index tick
            pos.premium = premium
            with account_scope(account), patch.object(algo_module.feed, "age", return_value=0):
                await engine._manage_position()
        return positions

    async def test_profit_rupee_condition_exits_both_accounts(self):
        positions = await self._rupee_exit(premium=200, profit=1000, loss=0)
        for account, pos in positions.items():
            self.assertGreaterEqual((200 - pos.entry_premium) * pos.quantity, 1000)
            self.assertEqual(pos.exit_reason, "PROFIT_RS")
            self.assertTrue(any(o["transactionType"] == "SELL" for o in self.brokers[account].orders))
        self.assertEqual([b.net for b in self.brokers.values()], [0, 0])

    async def test_loss_rupee_condition_exits_and_zero_means_off(self):
        positions = await self._rupee_exit(premium=1, profit=0, loss=500)
        for pos in positions.values():
            self.assertEqual(pos.exit_reason, "LOSS_RS")
        self.assertEqual([b.net for b in self.brokers.values()], [0, 0])

    async def _open_then_save(self, pnl_now, profit, loss):
        """Hold a live position at `pnl_now`, save rules mid-trade; NO tick runs after the save."""
        await self.dispatch_shared_signal()
        engine = self.manager.items["master"]
        engine.enabled = True
        pos = engine._position
        self.assertEqual(pos.execution_state, "open")

        def set_pnl(pnl):
            pos.spot = None
            pos.premium = pos.entry_premium + pnl / pos.quantity
            self.assertGreater(pos.premium, 0)

        async def tick(pnl):
            set_pnl(pnl)
            with account_scope("master"), patch.object(algo_module.feed, "age", return_value=0):
                await engine._manage_position()
            return pos.exit_reason

        self.assertFalse(await tick(pnl_now), "no rule saved yet -> must hold")
        sells_before = sum(o["transactionType"] == "SELL" for o in self.brokers["master"].orders)
        with patch.object(algo_module.feed, "age", return_value=0):
            r = await self.http.post("/api/algo/exit-rules", json={
                "profit_target_rs": profit, "stop_loss_rs": loss, "accounts": ["master"]})
        self.assertEqual(r.status_code, 200, r.text)
        sells = sum(o["transactionType"] == "SELL" for o in self.brokers["master"].orders) - sells_before
        return pos, r.json(), sells, tick

    async def test_scenario_profit_3000_then_save_1000_exits_on_save(self):
        pos, body, sells, _ = await self._open_then_save(3000, profit=1000, loss=0)
        self.assertEqual(body["exitTriggered"], {"master": "PROFIT_RS"})
        self.assertEqual((pos.exit_reason, sells, self.brokers["master"].net), ("PROFIT_RS", 1, 0))

    async def test_scenario_loss_3000_then_save_1000_exits_on_save(self):
        pos, body, sells, _ = await self._open_then_save(-3000, profit=0, loss=1000)
        self.assertEqual(body["exitTriggered"], {"master": "LOSS_RS"})
        self.assertEqual((pos.exit_reason, sells, self.brokers["master"].net), ("LOSS_RS", 1, 0))

    async def test_scenario_profit_800_with_1000_rule_exits_when_it_reaches_1000(self):
        pos, body, sells, tick = await self._open_then_save(800, profit=1000, loss=0)
        self.assertEqual((body["exitTriggered"], sells, pos.exit_reason), ({}, 0, ""))  # 800 holds
        self.assertGreater(self.brokers["master"].net, 0)
        self.assertEqual(await tick(999), "")
        self.assertEqual(await tick(1000), "PROFIT_RS")
        self.assertEqual(self.brokers["master"].net, 0)

    async def test_profit_1200_loss_0_never_exits_on_any_loss(self):
        pos, body, sells, tick = await self._open_then_save(0, profit=1200, loss=0)
        worst = -(pos.entry_premium - 0.05) * pos.quantity  # premium almost zero
        for pnl in (-1200, -5000, worst):
            self.assertEqual(await tick(pnl), "", pnl)
        self.assertGreater(self.brokers["master"].net, 0)
        self.assertEqual(await tick(1199), "")
        self.assertEqual(await tick(1200), "PROFIT_RS")

    async def test_profit_0_loss_1200_never_exits_on_any_profit(self):
        pos, body, sells, tick = await self._open_then_save(0, profit=0, loss=1200)
        for pnl in (1200, 50000, 500000):
            self.assertEqual(await tick(pnl), "", pnl)
        self.assertGreater(self.brokers["master"].net, 0)
        self.assertEqual(await tick(-1199), "")
        self.assertEqual(await tick(-1200), "LOSS_RS")
        self.assertEqual(self.brokers["master"].net, 0)

    async def test_both_zero_uses_old_logic_only(self):
        pos, body, sells, tick = await self._open_then_save(0, profit=0, loss=0)
        for pnl in (500000, -(pos.entry_premium - 0.05) * pos.quantity):
            self.assertEqual(await tick(pnl), "", pnl)  # no rupee exit either way
        self.assertGreater(self.brokers["master"].net, 0)
        engine = self.manager.items["master"]
        pos.spot = pos.target_spot  # old spot-target rule still fires
        pos.premium = pos.entry_premium  # not near the 50% software SL
        with account_scope("master"), patch.object(algo_module.feed, "age", return_value=0):
            await engine._manage_position()
        self.assertEqual((pos.exit_reason, self.brokers["master"].net), ("TARGET", 0))

    async def test_save_with_stale_premium_waits_for_fresh_tick(self):
        await self.dispatch_shared_signal()
        engine = self.manager.items["master"]
        pos = engine._position
        pos.premium = pos.entry_premium + 3000 / pos.quantity
        with patch.object(algo_module.feed, "age", return_value=None):
            r = await self.http.post("/api/algo/exit-rules", json={
                "profit_target_rs": 1000, "stop_loss_rs": 0, "accounts": ["master"]})
        self.assertEqual(r.json()["exitTriggered"], {})
        self.assertGreater(self.brokers["master"].net, 0)  # never exits on an unverified price
        with account_scope("master"), patch.object(algo_module.feed, "age", return_value=0):
            await engine._manage_position()
        self.assertEqual((pos.exit_reason, self.brokers["master"].net), ("PROFIT_RS", 0))

    async def test_rupee_conditions_off_or_stale_premium_never_exit(self):
        positions = await self._rupee_exit(premium=1, profit=0, loss=0)
        self.assertTrue(all(not p.exit_reason for p in positions.values()))
        engine = self.manager.items["master"]
        engine.cfg.stop_loss_rs = 500
        with account_scope("master"), patch.object(algo_module.feed, "age", return_value=None):
            await engine._manage_position()
        self.assertFalse(positions["master"].exit_reason)
        self.assertTrue(all(b.net > 0 for b in self.brokers.values()))
        r = await self.http.post("/api/algo/exit-rules", json={"profit_target_rs": -1, "stop_loss_rs": 0})
        self.assertEqual(r.status_code, 400)
        r = await self.http.post("/api/algo/exit-rules", json={"profit_target_rs": 1, "stop_loss_rs": 1,
                                                                "accounts": ["bogus"]})
        self.assertEqual(r.status_code, 400)

    async def test_batch_retry_after_restart_uses_persisted_receipt(self):
        first = (await self.http.post("/api/trading/place", json=self.body)).json()
        for c in client_module._trading_clients.values():
            c._read_cache.clear()
        second = (await self.http.post("/api/trading/place", json=self.body)).json()
        self.assertEqual(first, second)
        self.assertEqual([len(b.supers) for b in self.brokers.values()], [1, 1])

    async def test_bad_pin_has_no_broker_writes_and_no_success_receipt(self):
        self.patch.object(runtime, "_fast_orders", False)
        self.body["pin"] = "not-the-pin"
        response = await self.http.post("/api/trading/place", json=self.body)
        self.assertEqual(response.status_code, 403)
        self.assertTrue(all(not b.supers and not b.orders for b in self.brokers.values()))

    async def test_configured_child_cannot_be_selected_with_master_client_id_state(self):
        with account_scope("child"):
            algo_store.store.save_state({"clientId": "different-client", "enabled": True})
            with self.assertRaises(RuntimeError):
                self.manager.items["child"]._restore()

    async def test_mode_change_disarms_both_and_cannot_reuse_dry_run_receipt_as_live(self):
        runtime._dry_run = True
        await self.http.post("/api/trading/place", json=self.body)
        r = await self.http.post("/api/settings/dry-run", json={"value": False})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(algo_module.engines.entry_accounts, ())
        status = (await self.http.get("/api/trading/accounts")).json()
        self.assertFalse(any(a["algoEntryEnabled"] for a in status["accounts"]))
        r = await self.http.post("/api/trading/place", json=self.body)
        self.assertEqual(r.status_code, 409)
        self.assertTrue(all(not b.supers for b in self.brokers.values()))

    async def test_http_200_rejected_order_is_not_reported_success_and_does_not_rollback_master(self):
        self.brokers["child"].reject_status = True
        response = (await self.http.post("/api/trading/place", json=self.body)).json()
        self.assertFalse(response["ok"])
        self.assertTrue(response["results"][0]["ok"])
        self.assertFalse(response["results"][1]["ok"])
        self.assertIn("Risk validation", response["results"][1]["error"])
        self.assertEqual(self.brokers["master"].net, 65)
        self.assertEqual(self.brokers["child"].net, 0)

    async def test_partial_arm_preflight_keeps_healthy_account_armed(self):
        self.brokers["child"].ip_allowed = False
        r = await self.http.post("/api/algo/arm")
        result = r.json()
        self.assertFalse(result["ok"])
        self.assertTrue(result["armed"])
        self.assertEqual(algo_module.engines.entry_accounts, ("master",))
        self.assertTrue(self.manager.items["master"].enabled)
        self.assertFalse(self.manager.items["child"].enabled)
        self.assertIn("reject", self.manager.items["child"]._last_error)

    async def test_failed_arm_persistence_disarms_every_account(self):
        self.patch.object(self.manager.items["child"], "_persist", side_effect=OSError("disk full"))
        r = await self.http.post("/api/algo/arm")
        self.assertEqual(r.status_code, 503)
        self.assertFalse(any(e.enabled for e in self.manager.items.values()))
        self.assertEqual(algo_module.engines.entry_accounts, ())

    async def test_config_edit_checks_both_engines_and_requires_master_view(self):
        r = await self.http.post("/api/algo/config", json={"risk_pct": 1},
                                 headers={"X-Trading-Account": "child"})
        self.assertEqual(r.status_code, 409)
        self.manager.items["child"].enabled = True
        r = await self.http.post("/api/algo/config", json={"risk_pct": 1})
        self.assertEqual(r.status_code, 409)
        self.manager.items["child"].enabled = False
        r = await self.http.post("/api/algo/config", json={"risk_pct": 1})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.manager.items["master"].cfg.risk_pct, 1)

    async def test_child_static_ip_registration_uses_child_client_identity(self):
        with account_scope("child"):
            await client_module.get_trading_client().set_ip("192.0.2.1", "PRIMARY")
        self.assertFalse(self.brokers["master"].calls)
        self.assertEqual(self.brokers["child"].calls[-1][2]["dhanClientId"], "child-id")

    async def test_restart_restores_management_but_disarms_entries_and_uses_one_feed(self):
        for account, engine in self.manager.items.items():
            engine.enabled = True
            engine._signal_day = algo_module.now_ist().date().isoformat()
            with account_scope(account):
                engine._persist()
            engine.enabled = False
        with patch.object(algo_module.feed, "start", new_callable=AsyncMock) as start, \
             patch.object(algo_module.feed, "stop", new_callable=AsyncMock) as stop, \
             patch.object(algo_module.feed, "set_on_tick") as callback, \
             patch.object(algo_module.feed, "subscribe"):
            await self.manager.start()
            try:
                start.assert_awaited_once()
                callback.assert_called_once_with(self.manager.on_tick)
                self.assertFalse(any(e.enabled for e in self.manager.items.values()))
                self.assertEqual(self.manager.entry_accounts, ())
                self.assertTrue(all(e._signal_day == algo_module.now_ist().date().isoformat()
                                    for e in self.manager.items.values()))
                for account in accounts.ACCOUNT_IDS:
                    with account_scope(account):
                        self.assertFalse(algo_store.store.load_state()["enabled"])
            finally:
                await self.manager.stop()
            stop.assert_awaited_once()

    async def test_intraday_data_always_uses_master_data_token_in_either_account_context(self):
        data = DhanClient()
        await data._client.aclose()
        calls = []
        def handle(request):
            self.assertEqual(request.headers["client-id"], "master-id")
            self.assertEqual(request.headers["access-token"], "data-token")
            self.assertEqual(request.url.path, "/v2/charts/intraday")
            calls.append(request)
            return httpx.Response(200, json={"high": [23000]})
        data._client = httpx.AsyncClient(base_url="https://api.dhan.co/v2",
                                         transport=httpx.MockTransport(handle))
        self.addAsyncCleanup(data.aclose)
        with patch.object(algo_module, "get_data_client", return_value=data):
            for account, engine in self.manager.items.items():
                with account_scope(account):
                    result = await engine._intraday(security_id="13", exchange_segment="IDX_I",
                        instrument="INDEX", interval="1", from_dt="2026-10-26 09:15:00",
                        to_dt="2026-10-26 09:26:00", throttle=False)
                    self.assertEqual(result["high"], [23000])
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(not b.calls for b in self.brokers.values()))

    async def test_master_combined_token_and_distinct_child_trading_token(self):
        settings = Settings(
            _env_file=None, dhan_client_id="master-id", dhan_access_token="combined-master-token",
            dhan_master_trading_token="", dhan_child_client_id="child-id",
            dhan_child_access_token="child-trading-only-token",
        )
        self.patch("app.config.get_settings", return_value=settings)
        self.patch.object(client_module, "get_settings", return_value=settings)
        self.patch.object(client_module, "_trading_clients", {})
        calls = []

        def handle(request):
            calls.append((request.headers["client-id"], request.headers["access-token"],
                          request.url.path))
            return httpx.Response(200, json={"availabelBalance": 100000})

        data = DhanClient()
        await data._client.aclose()
        data._client = httpx.AsyncClient(base_url="https://api.dhan.co/v2",
                                         transport=httpx.MockTransport(handle))
        self.addAsyncCleanup(data.aclose)
        self.patch.object(client_module, "_client", data)
        for account, expected in (
            ("master", ("master-id", "combined-master-token")),
            ("child", ("child-id", "child-trading-only-token")),
        ):
            with account_scope(account):
                self.assertEqual(accounts.account_credentials(account), expected)
                trading = client_module.get_trading_client()
                await trading._client.aclose()
                trading._client = httpx.AsyncClient(base_url="https://api.dhan.co/v2",
                    transport=httpx.MockTransport(handle))
                self.addAsyncCleanup(trading.aclose)
                await trading.get_fund_limit()
                self.assertEqual(calls[-1][:2], expected)
                master_data = client_module.get_dhan_client()
                self.assertIs(master_data, data)
                await master_data._request("POST", "/marketfeed/ltp", json={"IDX_I": [13]})
                self.assertEqual(calls[-1][:2], ("master-id", "combined-master-token"))
        self.assertIsNot(client_module._trading_clients["master"],
                         client_module._trading_clients["child"])

    async def test_startup_auto_arm_arms_both_and_skips_ip_rejected_account(self):
        from app.routers import trading_accounts

        auto = Settings(_env_file=None, dhan_client_id="master-id", dhan_access_token="data-token",
                        dhan_master_trading_token="token-master-id",
                        dhan_child_client_id="child-id", dhan_child_access_token="token-child-id",
                        TRADING_DEFAULT_ACCOUNTS="master,child", algo_auto_arm=True)
        self.patch.object(trading_accounts, "get_settings", return_value=auto)
        self.patch.object(accounts, "_selected", ("master",))
        algo_module.engines.entry_accounts = ()
        for engine in self.manager.items.values():
            engine.enabled = False
        ip_ok = {"master": True, "child": True}
        for account, engine in self.manager.items.items():
            async def preflight(account=account):
                return {"ok": ip_ok[account], "reason": "test"}
            self.patch.object(engine, "_preflight_ip", new=preflight)
        result = await trading_accounts.apply_startup_defaults()
        self.assertTrue(result["ok"], result)
        self.assertEqual(accounts.selected_accounts(), ("master", "child"))
        self.assertEqual(algo_module.engines.entry_accounts, ("master", "child"))
        self.assertTrue(all(e.enabled for e in self.manager.items.values()))

        await self.http.post("/api/trading/algo", json={"action": "disarm"})
        ip_ok["child"] = False
        result = await trading_accounts.apply_startup_defaults()
        self.assertFalse(result["ok"])
        self.assertEqual(algo_module.engines.entry_accounts, ("master",))
        self.assertFalse(self.manager.items["child"].enabled)

    async def test_algo_exit_both_reports_pending_broker_error_not_false_success(self):
        for account, engine in self.manager.items.items():
            result = {"position": {"executionError": "Broker exit timed out"}} if account == "child" else {"position": None}
            self.patch.object(engine, "panic_exit", new=AsyncMock(return_value=result))
        result = (await self.http.post("/api/trading/algo", json={"action": "exit"})).json()
        self.assertFalse(result["ok"])
        self.assertTrue(result["results"][0]["ok"])
        self.assertFalse(result["results"][1]["ok"])
        self.assertIn("timed out", result["results"][1]["error"])

    async def test_shared_algo_toggle_status_does_not_follow_viewed_account(self):
        self.manager.items["master"].enabled = True
        self.manager.items["child"].enabled = False
        algo_module.engines.entry_accounts = ("master",)
        for account in accounts.ACCOUNT_IDS:
            response = await self.http.get("/api/algo/status",
                headers={"X-Trading-Account": account})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["sharedEnabled"])
            self.assertEqual(response.json()["sharedEntryAccounts"], ["master"])
            self.assertEqual(response.json()["enabled"], account == "master")

    async def test_ip_list_failure_does_not_crash_arm_or_block_healthy_child(self):
        self.patch.object(client_module._trading_clients["master"], "detect_public_ip",
                          new=AsyncMock(return_value="192.0.2.1"))
        self.brokers["master"].ip_response = [{"status": "failure", "message": "Static IP is not configured"}]
        result = await self.http.post("/api/algo/arm")
        self.assertEqual(result.status_code, 200)
        self.assertFalse(result.json()["ok"])
        self.assertTrue(result.json()["armed"])
        self.assertIn("Static IP is not configured", result.json()["results"][0]["error"])
        self.assertEqual(algo_module.engines.entry_accounts, ("child",))
        response = await self.http.get("/api/account/ip",
                                       headers={"X-Trading-Account": "master"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("Static IP is not configured", response.json()["registeredError"]["errorMessage"])

    async def test_ip_adapter_rejects_unknown_shapes_and_false_permission_strings(self):
        from app.dhan_client import DhanAPIError
        client = client_module._trading_clients["master"]
        for raw in ([], [{"status": "failure"}], {"message": "not configured"}, "invalid"):
            self.brokers["master"].ip_response = raw
            with self.assertRaises(DhanAPIError):
                await client.get_ip()
        self.brokers["master"].ip_response = {"ordersAllowed": "false", "ipMatchStatus": "MISMATCH"}
        with self.assertRaises(DhanAPIError):
            await client.get_ip()

    async def test_shared_arm_from_either_view_arms_both_selected_accounts(self):
        for view in accounts.ACCOUNT_IDS:
            result = (await self.http.post("/api/algo/arm",
                headers={"X-Trading-Account": view})).json()
            self.assertTrue(result["ok"], result)
            self.assertTrue(all(e.enabled for e in self.manager.items.values()))
            self.assertEqual(algo_module.engines.entry_accounts, ("master", "child"))
            response = await self.http.post("/api/algo/disarm",
                headers={"X-Trading-Account": view})
            self.assertEqual(response.status_code, 200)
            self.assertFalse(any(e.enabled for e in self.manager.items.values()))


class AccountConfigurationTests(unittest.TestCase):
    def test_duplicate_or_partial_child_credentials_rejected(self):
        from pydantic import ValidationError
        for changes in ({"dhan_child_client_id": "child"},
                        {"dhan_child_client_id": "same", "dhan_child_access_token": "token",
                         "dhan_client_id": "same"}):
            with self.assertRaises(ValidationError):
                Settings(_env_file=None, **changes)

    def test_trading_budgets_are_account_specific_data_budget_shared(self):
        from app.rate_limit import gate_for
        self.assertIsNot(gate_for("POST", "/orders", "a"), gate_for("POST", "/orders", "b"))
        self.assertIs(gate_for("POST", "/orders", "a"), gate_for("DELETE", "/orders/1", "a"))
        self.assertIs(gate_for("POST", "/charts/intraday", "a"), gate_for("POST", "/charts/intraday", "b"))


if __name__ == "__main__":
    unittest.main()
