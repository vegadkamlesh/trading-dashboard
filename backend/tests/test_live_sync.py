import asyncio
import importlib
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from app.dhan_client import DhanAPIError, DhanClient
from app.history import HistoryEngine
from app.instruments import get_index
from app.rate_limit import chain_gate_for


class LiveSyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = DhanClient()
        self.client._request = AsyncMock()
        self.addAsyncCleanup(self.client.aclose)

    async def test_singleton_detail_is_normalized_with_exact_identity(self):
        self.client._request.return_value = [{
            "orderId": "order-1", "correlationId": "request-1", "orderStatus": "TRADED",
            "filledQty": 65, "averageTradedPrice": 100,
        }]
        self.assertEqual((await self.client.get_order_by_id("order-1"))["filledQty"], 65)
        self.assertEqual((await self.client.get_order_by_correlation("request-1"))["orderId"], "order-1")

    async def test_empty_wrong_duplicate_or_missing_status_order_never_becomes_fill(self):
        self.client.get_order_book = AsyncMock(return_value=[])
        for raw in ([], {}, [{"orderId": "wrong", "orderStatus": "TRADED"}],
                    [{"orderId": "order-1"}],
                    [{"orderId": "order-1", "orderStatus": "TRADED"}] * 2):
            with self.subTest(raw=raw):
                self.client._request.return_value = raw
                with self.assertRaises(DhanAPIError) as caught:
                    await self.client.get_order_by_id("order-1")
                self.assertEqual(caught.exception.error_code, "UNKNOWN_ORDER")

    async def test_missing_detail_can_use_exact_fresh_current_book(self):
        self.client._request.return_value = []
        row = {"orderId": "order-1", "correlationId": "request-1", "orderStatus": "TRADED", "filledQty": 65}
        self.client.get_order_book = AsyncMock(return_value=[
            {"orderId": "another", "orderStatus": "TRADED", "filledQty": 100}, row,
        ])
        self.assertEqual(await self.client.get_order_by_id("order-1"), row)
        self.assertEqual(await self.client.get_order_by_correlation("request-1"), row)
        self.client.get_order_book.assert_awaited_with(fresh=True)

    async def test_detail_auth_error_does_not_fallback_to_cached_book(self):
        self.client._request.side_effect = DhanAPIError(401, "DH-901", "Invalid token")
        self.client.get_order_book = AsyncMock(return_value=[{"orderId": "order-1", "orderStatus": "TRADED"}])
        with self.assertRaises(DhanAPIError):
            await self.client.get_order_by_id("order-1")
        self.client.get_order_book.assert_not_awaited()

    async def test_broker_snapshot_failure_evicts_old_success_and_reports_timestamp(self):
        self.client._request.return_value = [{
            "securityId": "1", "netQty": 65, "exchangeSegment": "NSE_FNO", "productType": "INTRADAY",
        }]
        await self.client.get_positions()
        observed = self.client.sync_status()["observedAt"]["/positions"]
        self.client._request.side_effect = DhanAPIError(401, "DH-901", "Token expired")
        with self.assertRaises(DhanAPIError):
            await self.client.get_positions(fresh=True)
        self.assertNotIn("/positions", self.client._read_cache)
        status = self.client.sync_status()
        self.assertEqual(status["errors"]["/positions"], "Token expired")
        self.assertEqual(status["observedAt"]["/positions"], observed)
        with self.assertRaises(DhanAPIError):
            await self.client.get_positions()

    async def test_message_only_books_do_not_report_flat(self):
        for raw in ({}, [{"message": "Something went wrong"}], [{"securityId": "1"}]):
            self.client._request.return_value = raw
            with self.assertRaises(DhanAPIError):
                await self.client.get_positions(fresh=True)
        self.assertIn("/positions", self.client.sync_status()["errors"])

    async def test_order_event_invalidates_not_infers_holdings(self):
        self.client._read_cache["/positions"] = (0, [{"netQty": 65}])
        self.client._on_order_update({"Type": "order_alert", "Data": {
            "ClientId": self.client._client_id, "Status": "Traded", "TradedQty": 65,
        }})
        self.assertEqual(self.client._read_cache, {})
        self.assertTrue(self.client._refresh_event.is_set())
        self.assertNotIn("/positions", self.client.sync_status()["observedAt"])
        self.client._request.assert_not_awaited()

    async def test_wrong_account_event_does_not_invalidate(self):
        self.client._read_cache["/positions"] = (0, [])
        with self.assertRaises(DhanAPIError):
            self.client._on_order_update({"Type": "order_alert", "Data": {
                "ClientId": "wrong-account", "Status": "Traded",
            }})
        self.assertIn("/positions", self.client._read_cache)
        self.assertFalse(self.client._refresh_event.is_set())

    async def test_poll_updates_all_resources_even_when_one_fails(self):
        def broker(method, path):
            if path == "/orders":
                raise DhanAPIError(0, "BAD_BROKER_BOOK", "Order book unavailable")
            return {"availabelBalance": 50000} if path == "/fundlimit" else []
        self.client._request.side_effect = broker
        await self.client._account_poll_once()
        self.assertEqual(self.client._request.await_count, 4)
        self.assertEqual(set(self.client.sync_status()["observedAt"]),
                         {"/positions", "/super/orders", "/fundlimit"})
        self.assertIn("/orders", self.client.sync_status()["errors"])

    async def test_account_stream_auth_and_event_refresh(self):
        message = {"Type": "order_alert", "Data": {
            "ClientId": self.client._client_id, "Status": "Cancelled",
        }}
        ws = MagicMock()
        ws.send = AsyncMock()
        ws.__aiter__.return_value = [json.dumps(message)]
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=ws)
        context.__aexit__ = AsyncMock()
        self.client._refresh_event.clear()
        with patch("app.dhan_client.order_ws_connect", return_value=context):
            task = asyncio.create_task(self.client._order_update_loop())
            try:
                await asyncio.wait_for(self.client._refresh_event.wait(), timeout=1)
                await asyncio.sleep(0)
                login = json.loads(ws.send.call_args.args[0])
                self.assertEqual(login["LoginReq"]["ClientId"], self.client._client_id)
                self.assertEqual(login["LoginReq"]["MsgCode"], 42)
                self.assertEqual(login["UserType"], "SELF")
                self.assertIsNotNone(self.client._last_order_event)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_close_cancels_live_tasks(self):
        task = asyncio.create_task(asyncio.sleep(100))
        self.client._live_tasks = [task]
        await self.client.aclose()
        self.assertTrue(task.cancelled())
        self.assertEqual(self.client._live_tasks, [])

    async def test_immediate_order_stream_close_preserves_exponential_backoff(self):
        ws = MagicMock(send=AsyncMock())
        ws.__aiter__.return_value = []
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=ws)
        context.__aexit__ = AsyncMock()
        delays = []
        async def sleep(delay):
            delays.append(delay)
            if len(delays) == 2:
                raise asyncio.CancelledError()
        with patch("app.dhan_client.order_ws_connect", return_value=context), \
                patch("app.dhan_client.asyncio.sleep", side_effect=sleep):
            with self.assertRaises(asyncio.CancelledError):
                await self.client._order_update_loop()
        self.assertEqual(delays, [1, 2])
        self.assertFalse(self.client._order_connected)

    async def test_unique_chain_requests_share_only_matching_gate(self):
        one = chain_gate_for("client", "13", "2026-11-03")
        self.assertIs(one, chain_gate_for("client", "13", "2026-11-03"))
        self.assertIsNot(one, chain_gate_for("client", "25", "2026-11-03"))
        self.assertIsNot(one, chain_gate_for("client", "13", "2026-11-10"))
        self.assertEqual(one.gap, 3.1)

    async def test_history_bad_request_is_explicit_and_not_repeated_for_every_chunk(self):
        self.client.get_rolling_expired_options = AsyncMock(
            side_effect=DhanAPIError(400, "DH-905", "Bad parameters"))
        with patch("app.history.get_dhan_client", return_value=self.client):
            result = await HistoryEngine()._compute(get_index("NIFTY"), "CALL", "ATM", 90, "WEEK")
        self.client.get_rolling_expired_options.assert_awaited_once()
        self.assertEqual(result.sample_days, 0)
        self.assertIsNone(result.contract_win_rate)
        self.assertIn("DH-905", result.error)


class AuthAndReconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_blank_401_is_actionable_and_request_cooldown_prevents_flood(self):
        client = DhanClient()
        await client._client.aclose()
        client._mock = False
        handler = MagicMock(return_value=httpx.Response(401, content=b""))
        client._client = httpx.AsyncClient(base_url="https://api.dhan.co/v2",
                                          transport=httpx.MockTransport(handler))
        gate = MagicMock(acquire=AsyncMock())
        try:
            with patch("app.dhan_client.gate_for", return_value=gate):
                for _ in range(3):
                    with self.assertRaises(DhanAPIError) as caught:
                        await client.get_positions(fresh=True)
                    self.assertEqual(caught.exception.error_code, "DH-901")
            handler.assert_called_once()
        finally:
            await client.aclose()

    async def test_immediate_market_socket_close_does_not_reset_backoff(self):
        module = importlib.import_module("app.feed")
        feed = module.MarketFeed()
        feed._running = True
        feed._push_subscription = AsyncMock()
        ws = MagicMock()
        ws.__aiter__.return_value = []
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=ws)
        context.__aexit__ = AsyncMock()
        waits = []
        async def sleep(delay):
            waits.append(delay)
            if len(waits) == 2:
                feed._running = False
        settings = MagicMock(dhan_access_token="test-token", dhan_client_id="test-client")
        with patch.object(module, "get_settings", return_value=settings), \
                patch.object(module, "ws_connect", return_value=context), \
                patch.object(module.asyncio, "sleep", side_effect=sleep):
            await feed._ws_loop()
        self.assertEqual(waits, [1, 2])
