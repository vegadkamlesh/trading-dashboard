"""Async wrapper over the DhanHQ v2 REST API.

Only the endpoints this dashboard needs are implemented. Every method:
  * injects the access-token + client-id headers (kept server-side),
  * maps Dhan error payloads into a uniform `DhanAPIError`,
  * never logs secrets.

MOCK MODE
---------
If DHAN_ACCESS_TOKEN starts with "mock", the client returns synthetic but
realistic data and never touches the network. This lets you explore the full UI
(option chain, advisor, history, news) without real credentials.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import json as json_codec
import math
import math
import random
import socket
import time
from copy import deepcopy
from typing import Any, Dict, List, Optional

import httpx
try:
    from websockets.asyncio.client import connect as order_ws_connect
except ImportError:
    from websockets.client import connect as order_ws_connect

from .config import get_settings
from .rate_limit import DATA_GATE, chain_gate_for, gate_for

logger = logging.getLogger("dhan.client")

BASE_URL = "https://api.dhan.co/v2"

MOCK_TOKEN_PREFIX = "mock"


def is_mock_token(token: str) -> bool:
    return token.lower().startswith(MOCK_TOKEN_PREFIX)


# ---------------------------------------------------------------------------
# Force IPv4 for ALL outbound calls.
#
# Why: Dhan whitelists ONE static IP for live orders. On dual-stack networks
# (most Indian ISPs = IPv4 + IPv6), Python may egress over IPv6, so Dhan sees a
# different source IP than the whitelisted IPv4 and blocks orders with
# "Invalid IP" / ipMatchStatus: MISMATCH. Pinning every DNS lookup to AF_INET
# guarantees we always reach Dhan from the whitelisted IPv4 address.
# ---------------------------------------------------------------------------
_ORIG_GETADDRINFO = socket.getaddrinfo


def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _ORIG_GETADDRINFO(host, port, socket.AF_INET, type, proto, flags)


def source_ip_for(account_id: str) -> str:
    """Local IPv4 this account's Dhan traffic must leave from ("" = OS default)."""
    s = get_settings()
    if account_id == "child":
        return s.dhan_child_source_ip.strip()
    if account_id in ("master", "master-data"):
        return s.dhan_master_source_ip.strip()
    return ""


def _transport(source_ip: str) -> Optional[httpx.AsyncHTTPTransport]:
    return httpx.AsyncHTTPTransport(local_address=source_ip) if source_ip else None


def enable_ipv4_only() -> None:
    """Patch socket resolution to IPv4 only (idempotent)."""
    if socket.getaddrinfo is not _ipv4_only_getaddrinfo:
        socket.getaddrinfo = _ipv4_only_getaddrinfo
        logger.info("Outbound connections pinned to IPv4 (Dhan static-IP safe)")


# Endpoints that are heaviest on Dhan's rate limits. We serialise these through a
# single global gate so that concurrent callers (guidance + seasonality +
# history all loading on a page open) can never burst and trigger a 429.
_HEAVY_PATHS = ("/charts/historical", "/charts/rollingoption", "/charts/intraday")
# Minimum gap between two heavy calls. Dhan's data API is strict; ~1.1s keeps us
# comfortably under the per-second limits even with several panels loading.
_HEAVY_MIN_GAP_SECONDS = 1.1

# Dhan error codes we surface specially in the UI (see Annexure).
ERROR_HINTS = {
    "DH-901": "Access token / client ID invalid or expired.",
    "DH-902": "Data API not subscribed or trading API access missing.",
    "DH-903": "Account/segment issue. Check Static IP whitelisting & segments.",
    "DH-904": "Rate limit breached. Slow down requests.",
    "DH-905": "Invalid input parameters.",
    "DH-906": "Order rejected / cannot be processed.",
    "DH-907": "System unable to fetch data for given parameters.",
    "DH-908": "Internal server error at Dhan.",
    "DH-909": "Network error talking to Dhan.",
    "DH-910": "Order failed for other reasons.",
}


class DhanAPIError(Exception):
    def __init__(self, status_code: int, error_code: str, error_message: str):
        self.status_code = status_code
        self.error_code = error_code
        self.error_message = error_message
        super().__init__(f"[{error_code}] {error_message}")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "statusCode": self.status_code,
            "errorCode": self.error_code,
            "errorMessage": self.error_message,
            "hint": ERROR_HINTS.get(self.error_code, ""),
        }


class DhanClient:
    # Class-level gate shared by ALL calls (heavy endpoints are global-limited).
    _heavy_lock: Optional[asyncio.Lock] = None
    _last_heavy_at: float = 0.0

    def __init__(self, timeout: float = 15.0, *, client_id: Optional[str] = None,
                 access_token: Optional[str] = None, account_id: str = "master-data") -> None:
        # Pin outbound traffic to IPv4 so Dhan always sees the whitelisted IP.
        enable_ipv4_only()
        s = get_settings()
        self.account_id = account_id
        self._client_id = s.dhan_client_id if client_id is None else client_id
        token = s.dhan_access_token if access_token is None else access_token
        self._headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "access-token": token,
            "client-id": self._client_id,
        }
        self.source_ip = source_ip_for(account_id)
        self._client = httpx.AsyncClient(base_url=BASE_URL, timeout=timeout, transport=_transport(self.source_ip))
        if self.source_ip:
            logger.info("account=%s outbound Dhan traffic bound to %s", account_id, self.source_ip)
        self._mock = is_mock_token(token)
        self._read_cache: Dict[str, tuple[float, Any]] = {}
        self._read_locks: Dict[str, asyncio.Lock] = {}
        self._observed_at: Dict[str, float] = {}
        self._read_errors: Dict[str, str] = {}
        self._live_tasks: list[asyncio.Task] = []
        self._refresh_event = asyncio.Event()
        self._order_connected = False
        self._order_error: Optional[str] = None
        self._last_order_event: Optional[float] = None
        self._auth_retry_at = 0.0
        self._auth_error: Optional[DhanAPIError] = None
        if self._mock:
            logger.warning("DhanClient running in MOCK mode (synthetic data, no network)")

    async def aclose(self) -> None:
        for task in self._live_tasks:
            task.cancel()
        if self._live_tasks:
            await asyncio.gather(*self._live_tasks, return_exceptions=True)
            self._live_tasks.clear()
        await self._client.aclose()

    def start_live_sync(self) -> None:
        if self._live_tasks or self._mock:
            return
        self._live_tasks = [
            asyncio.create_task(self._account_poll_loop(), name=f"{self.account_id}-broker-poll"),
            asyncio.create_task(self._order_update_loop(), name=f"{self.account_id}-order-stream"),
        ]

    def sync_status(self) -> Dict[str, Any]:
        return {
            "accountId": self.account_id, "source": "dhan",
            "observedAt": dict(self._observed_at), "errors": dict(self._read_errors),
            "orderStreamConnected": self._order_connected, "orderStreamError": self._order_error,
            "lastOrderEventAt": self._last_order_event,
        }

    def _on_order_update(self, raw: Any) -> None:
        if not isinstance(raw, dict) or raw.get("Type") != "order_alert":
            return
        data = raw.get("Data")
        if not isinstance(data, dict) or str(data.get("ClientId")) != self._client_id:
            raise DhanAPIError(0, "BAD_ORDER_EVENT", "Order stream identity missing/mismatched; event ignored.")
        self._last_order_event = time.time()
        self._read_cache.clear()
        self._refresh_event.set()
        logger.info("account=%s external_order_event status=%s; refreshing broker books",
                    self.account_id, data.get("Status"), extra={
                        "telegram_event": "broker_update", "telegram_account": self.account_id,
                    })

    async def _account_poll_once(self) -> None:
        for path, call in (
            ("/positions", lambda: self.get_positions(fresh=True)),
            ("/orders", lambda: self.get_order_book(fresh=True)),
            ("/super/orders", lambda: self.get_super_orders(fresh=True)),
            ("/fundlimit", lambda: self.get_fund_limit(fresh=True)),
        ):
            previous_error = self._read_errors.get(path)
            try:
                await call()
                if previous_error:
                    logger.info("account=%s resource=%s live_account_sync recovered", self.account_id, path)
            except DhanAPIError as exc:
                if previous_error != exc.error_message:
                    logger.warning("account=%s resource=%s live_account_sync code=%s reason=%s",
                                   self.account_id, path, exc.error_code, exc.error_message)

    async def _account_poll_loop(self) -> None:
        while True:
            self._refresh_event.clear()
            next_poll = time.monotonic() + 1.0
            await self._account_poll_once()
            try:
                await asyncio.wait_for(self._refresh_event.wait(), timeout=max(.1, next_poll - time.monotonic()))
            except asyncio.TimeoutError:
                pass

    async def _order_update_loop(self) -> None:
        delay = 1.0
        while True:
            try:
                bind = {"local_addr": (self.source_ip, 0)} if self.source_ip else {}
                async with order_ws_connect("wss://api-order-update.dhan.co",
                                            ping_interval=20, ping_timeout=20, **bind) as ws:
                    await ws.send(json_codec.dumps({
                        "LoginReq": {"MsgCode": 42, "ClientId": self._client_id,
                                     "Token": self._headers["access-token"]},
                        "UserType": "SELF",
                    }))
                    self._order_connected = True
                    self._order_error = None
                    connected_at = time.monotonic()
                    self._refresh_event.set()
                    async for message in ws:
                        self._on_order_update(json_codec.loads(message))
                        if time.monotonic() - connected_at > 30:
                            delay = 1.0
            except asyncio.CancelledError:
                raise
            except (OSError, ValueError, DhanAPIError, httpx.HTTPError) as exc:
                self._order_error = f"{type(exc).__name__}: order stream unavailable; REST polling continues"
                logger.warning("account=%s order_stream_unavailable type=%s retry_in=%s",
                               self.account_id, type(exc).__name__, delay)
            except Exception:
                self._order_error = "Unexpected order-stream failure; REST polling continues"
                logger.exception("account=%s order stream failed", self.account_id)
            finally:
                self._order_connected = False
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    @classmethod
    async def _await_heavy_slot(cls) -> None:
        """Block until it's safe to make another heavy (chart) call.

        Uses a class-level lock + last-call timestamp so ALL callers (across
        tasks/panels) take turns, never overlapping and never bursting.
        """
        if cls._heavy_lock is None:
            cls._heavy_lock = asyncio.Lock()
        async with cls._heavy_lock:
            now = time.monotonic()
            wait = _HEAVY_MIN_GAP_SECONDS - (now - cls._last_heavy_at)
            if wait > 0:
                await asyncio.sleep(wait)
            cls._last_heavy_at = time.monotonic()

    # ---------------- low level ----------------
    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Optional[Dict[str, Any]] = None,
        throttle: bool = True,
    ) -> Any:
        if self._mock:
            return self._mock_response(method, path, json or {})
        if self._auth_error and time.monotonic() < self._auth_retry_at:
            raise DhanAPIError(self._auth_error.status_code, self._auth_error.error_code,
                               self._auth_error.error_message + " Retry paused for 60s; refresh credentials.")
        if self.account_id in {"master", "child"} and json is not None and (
                path.startswith("/orders") or path.startswith("/super/orders")):
            json = {**json, "dhanClientId": self._client_id}

        # Serialise heavy chart calls through one gate so parallel panels can't
        # burst the data API and trip Dhan's rate limit (DH-904 / 429).

        # `throttle=False` is used only by bulk back-fill jobs that do their own
        # bounded-concurrency + 429-retry management.
        if throttle and any(p in path for p in _HEAVY_PATHS):
            await self._await_heavy_slot()
        gate = gate_for(method, path, self._client_id)
        if path == "/optionchain" and json is not None:
            gate = chain_gate_for(self._client_id, str(json.get("UnderlyingScrip")), str(json.get("Expiry")))
            await DATA_GATE.acquire()
        await gate.acquire()

        try:
            resp = await self._client.request(
                method, path, headers=self._headers, json=json
            )
        except httpx.HTTPError as exc:  # network-level failure
            logger.warning("account=%s method=%s path=%s network_error=%s", self.account_id, method, path, exc)
            raise DhanAPIError(0, "DH-909", f"Network error: {exc}") from exc

        if resp.status_code >= 400:
            code, msg = self._extract_error(resp)
            if resp.status_code == 401:
                self._auth_error = DhanAPIError(resp.status_code, code, msg)
                self._auth_retry_at = time.monotonic() + 60
            if resp.status_code == 429 or code == "DH-904":
                try:
                    cooldown = max(3.0, float(resp.headers.get("Retry-After", "3")))
                except ValueError:
                    cooldown = 3.0
                gate.defer(cooldown)
            logger.warning("account=%s method=%s path=%s http=%s code=%s reason=%s",
                           self.account_id, method, path, resp.status_code, code, msg)
            raise DhanAPIError(resp.status_code, code, msg)

        if resp.status_code == 202 or not resp.content:
            return {"status": "accepted"}
        try:
            data = resp.json()
        except ValueError as exc:
            raise DhanAPIError(resp.status_code, "BAD_RESPONSE", "Broker returned non-JSON response; outcome unknown.") from exc
        self._auth_error = None
        return data

    async def _cached_read(self, path: str, *, fresh: bool = False) -> Any:
        async with self._read_locks.setdefault(path, asyncio.Lock()):
            cached = self._read_cache.get(path)
            if not fresh and cached and time.monotonic() - cached[0] < 1.0:
                return deepcopy(cached[1])
            try:
                result = await self._request("GET", path)
                if path in {"/positions", "/orders", "/super/orders"}:
                    if not isinstance(result, list) or any(not isinstance(row, dict) for row in result):
                        raise DhanAPIError(0, "BAD_BROKER_BOOK", f"Invalid {path} response; holdings/orders unknown.")
                    required = "netQty" if path == "/positions" else "orderStatus"
                    fields = (required, "securityId", "exchangeSegment", "productType")
                    if any(any(field not in row or row[field] is None for field in fields) for row in result):
                        raise DhanAPIError(0, "BAD_BROKER_BOOK", f"Missing identity/status/quantity in {path}; not a valid broker book.")
                    if path == "/positions":
                        try:
                            for row in result:
                                quantity = row["netQty"]
                                if isinstance(quantity, bool) or int(quantity) != float(quantity):
                                    raise ValueError("Non-integer net quantity")
                        except (TypeError, ValueError, OverflowError) as exc:
                            raise DhanAPIError(0, "BAD_BROKER_BOOK", "Invalid broker netQty; holdings unknown.") from exc
                elif path == "/fundlimit":
                    try:
                        if not isinstance(result, dict) or isinstance(result.get("availabelBalance"), bool):
                            raise ValueError("Invalid balance type")
                        if not math.isfinite(float(result["availabelBalance"])):
                            raise ValueError("Balance is not finite")
                    except (KeyError, TypeError, ValueError) as exc:
                        raise DhanAPIError(0, "BAD_FUNDS", "Invalid/missing available funds; balance unknown.") from exc
            except DhanAPIError as exc:
                self._read_cache.pop(path, None)
                self._read_errors[path] = exc.error_message
                raise
            self._read_cache[path] = (time.monotonic(), result)
            self._observed_at[path] = time.time()
            self._read_errors.pop(path, None)
            return deepcopy(result)

    @staticmethod
    def _extract_error(resp: httpx.Response) -> tuple[str, str]:
        try:
            data = resp.json()
        except ValueError:
            if resp.status_code == 401:
                return "DH-901", "Authentication rejected: token/client ID invalid or expired."
            return "DH-910", resp.text[:300] or f"Broker request failed (HTTP {resp.status_code})."
        if not isinstance(data, dict):
            data = next((r for r in data if isinstance(r, dict)), {}) if isinstance(data, list) else {}
        code = str(data.get("errorCode") or data.get("error_code") or
                   ("DH-901" if resp.status_code == 401 else "DH-910"))
        msg = str(
            data.get("errorMessage")
            or data.get("error_message")
            or data.get("message")
            or ("Authentication rejected: token/client ID invalid or expired." if resp.status_code == 401
                else f"Broker request failed (HTTP {resp.status_code}); no detailed reason supplied.")
        )
        return code, msg

    # ---------------- mock data ----------------
    def _mock_response(self, method: str, path: str, body: Dict[str, Any]) -> Any:
        if path.endswith("/expirylist"):
            return {"data": ["2025-12-25", "2026-01-01", "2026-01-30"]}
        if path.endswith("/optionchain") and "expirylist" not in path:
            return {"data": self._mock_chain()}
        if path.endswith("/charts/historical"):
            return self._mock_historical()
        if path.endswith("/charts/intraday"):
            return self._mock_intraday(body.get("fromDate", ""), body.get("toDate", ""))
        if path.endswith("/charts/rollingoption"):
            return self._mock_rolling(body.get("drvOptionType", "CALL"))
        if path.endswith("/profile"):
            return {
                "dhanClientId": self._client_id or "1000000000",
                "dhanClientName": "MOCK TRADER",
                "activeSegment": "NSE_FNO",
                "ddpi": True,
                "dataPlan": "Data APIs (mock)",
                "tokenValidity": "2025-12-31",
            }
        if path.endswith("/fundlimit"):
            return {
                "dhanClientId": self._client_id or "1000000000",
                "availabelBalance": 250000.0,
                "sodLimit": 300000.0,
                "collateralAmount": 0.0,
                "utilizableAmount": 0.0,
                "receiveableAmount": 0.0,
                "utilizedAmount": 12000.0,
                "blockedPayoutAmount": 0.0,
                "withdrawableBalance": 248000.0,
            }
        if path.endswith("/positions"):
            return [
                {
                    "tradingSymbol": "NIFTY 23500 CE",
                    "securityId": "99901",
                    "positionType": "LONG",
                    "productType": "INTRADAY",
                    "exchangeSegment": "NSE_FNO",
                    "netQty": 50,
                    "buyAvg": 120.5,
                    "sellAvg": 0.0,
                    "realizedProfit": 0.0,
                    "unrealizedProfit": 1225.0,
                    "drvOptionType": "CALL",
                    "drvStrikePrice": 23500,
                }
            ]
        if path.endswith("/holdings"):
            return []
        if path.endswith("/getIP"):
            return {"primaryIP": "203.0.113.7", "secondaryIP": "", "status": "SUCCESS"}
        if path.endswith("/super/orders") and method == "GET":
            return []
        if path.endswith("/orders") or path.endswith("/trades"):
            return []
        if method == "POST" and "/super/orders" in path:
            return {
                "orderId": str(random.randint(10**11, 10**12)),
                "orderStatus": "PENDING",
            }
        return {}

    @staticmethod
    def _mock_chain() -> Dict[str, Any]:
        spot = 23500.0
        oc: Dict[str, Any] = {}
        for k in range(-15, 16):
            strike = spot + k * 50
            dist = abs(k) * 12 + 8
            ce_ltp = max(2.0, 220 - k * 14 + random.uniform(-3, 3))
            pe_ltp = max(2.0, 220 + k * 14 + random.uniform(-3, 3))
            oc[str(strike)] = {
                "ce": {
                    "security_id": int(strike * 100 + 1),
                    "last_price": round(ce_ltp, 2),
                    "previous_close_price": round(ce_ltp + 5, 2),
                    "oi": 1200000 + k * 3000,
                    "previous_oi": 1180000 + k * 3000,
                    "volume": 50000,
                    "previous_volume": 45000,
                    "average_price": round(ce_ltp, 2),
                    "implied_volatility": round(14.5 + dist * 0.1, 2),
                    "top_bid_price": round(ce_ltp - 0.1, 2),
                    "top_bid_quantity": 750,
                    "top_ask_price": round(ce_ltp + 0.1, 2),
                    "top_ask_quantity": 900,
                    "greeks": {"delta": 0.5, "theta": -12.0, "gamma": 0.001, "vega": 8.0},
                },
                "pe": {
                    "security_id": int(strike * 100 + 2),
                    "last_price": round(pe_ltp, 2),
                    "previous_close_price": round(pe_ltp + 5, 2),
                    "oi": 900000 - k * 2000,
                    "previous_oi": 880000 - k * 2000,
                    "volume": 40000,
                    "previous_volume": 38000,
                    "average_price": round(pe_ltp, 2),
                    "implied_volatility": round(15.0 + dist * 0.1, 2),
                    "top_bid_price": round(pe_ltp - 0.1, 2),
                    "top_bid_quantity": 800,
                    "top_ask_price": round(pe_ltp + 0.1, 2),
                    "top_ask_quantity": 850,
                    "greeks": {"delta": -0.5, "theta": -11.0, "gamma": 0.001, "vega": 8.0},
                },
            }
        return {"last_price": spot, "oc": oc}

    @staticmethod
    def _mock_intraday(from_dt: str, to_dt: str) -> Dict[str, Any]:
        """Synthetic 1-min candles: per day, the 09:15-09:25 window + a close.

        Enough for the opening-range study to produce meaningful mock stats.
        """
        def _parse(s: str) -> Optional[_dt.datetime]:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    return _dt.datetime.strptime(s, fmt)
                except (ValueError, TypeError):
                    continue
            return None

        start = _parse(from_dt) or _dt.datetime.now() - _dt.timedelta(days=90)
        end = _parse(to_dt) or _dt.datetime.now()
        out: Dict[str, List[Any]] = {
            "open": [], "high": [], "low": [], "close": [], "volume": [], "timestamp": []
        }
        day = start.date()
        while day <= end.date():
            if day.weekday() < 5:  # skip weekends
                base = 23000.0 + random.uniform(-400, 400)
                # 09:16 .. 09:25 candles
                px = base
                for minute in range(16, 26):
                    o = px
                    c = o + random.uniform(-25, 25)
                    hi = max(o, c) + random.uniform(0, 20)
                    lo = min(o, c) - random.uniform(0, 20)
                    ts = int(_dt.datetime.combine(day, _dt.time(9, minute)).timestamp())
                    out["open"].append(round(o, 2))
                    out["high"].append(round(hi, 2))
                    out["low"].append(round(lo, 2))
                    out["close"].append(round(c, 2))
                    out["volume"].append(random.randint(1000, 50000))
                    out["timestamp"].append(ts)
                    px = c
                # 15:30 close candle
                ts = int(_dt.datetime.combine(day, _dt.time(15, 30)).timestamp())
                cl = base + random.uniform(-120, 120)
                out["open"].append(round(base, 2))
                out["high"].append(round(max(base, cl) + 20, 2))
                out["low"].append(round(min(base, cl) - 20, 2))
                out["close"].append(round(cl, 2))
                out["volume"].append(random.randint(1000, 50000))
                out["timestamp"].append(ts)
            day += _dt.timedelta(days=1)
        return out

    @staticmethod
    def _mock_historical(days: int = 200) -> Dict[str, Any]:
        opens: List[float] = []
        highs: List[float] = []
        lows: List[float] = []
        closes: List[float] = []
        vols: List[int] = []
        ts: List[int] = []
        price = 22000.0
        start = _dt.date.today() - _dt.timedelta(days=days)
        for i in range(days):
            drift = 12 * math.sin(i / 9.0) + 6 * math.cos(i / 4.0)
            o = price
            c = max(1000.0, price + drift + random.uniform(-60, 60))
            h = max(o, c) + random.uniform(5, 80)
            lo = min(o, c) - random.uniform(5, 80)
            opens.append(round(o, 2))
            highs.append(round(h, 2))
            lows.append(round(lo, 2))
            closes.append(round(c, 2))
            vols.append(random.randint(100000, 300000))
            ts.append(
                int(
                    _dt.datetime.combine(
                        start + _dt.timedelta(days=i), _dt.time(15, 30)
                    ).timestamp()
                )
            )
            price = c
        return {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": vols,
            "timestamp": ts,
        }

    @staticmethod
    def _mock_rolling(drv_option_type: str) -> Dict[str, Any]:
        """Synthetic expired-options minute data across ~20 sessions."""
        leg: Dict[str, List[Any]] = {
            "open": [],
            "high": [],
            "low": [],
            "close": [],
            "iv": [],
            "spot": [],
            "volume": [],
            "timestamp": [],
        }
        price = 200.0
        start = _dt.date.today() - _dt.timedelta(days=40)
        for day in range(20):
            base = price + random.uniform(-15, 15)
            drift = 2 if day % 3 else -2
            for minute in range(0, 370, 5):
                o = base
                c = max(1.0, base + random.uniform(-8, 8) + drift)
                h = max(o, c) + random.uniform(1, 5)
                lo = min(o, c) - random.uniform(1, 5)
                leg["open"].append(round(o, 2))
                leg["high"].append(round(h, 2))
                leg["low"].append(round(lo, 2))
                leg["close"].append(round(c, 2))
                leg["iv"].append(round(15 + random.uniform(-3, 3), 2))
                leg["spot"].append(round(23500 + random.uniform(-150, 150), 2))
                leg["volume"].append(random.randint(100, 5000))
                d = start + _dt.timedelta(days=day)
                leg["timestamp"].append(
                    int(_dt.datetime.combine(d, _dt.time(9, 15)).timestamp())
                    + minute * 60
                )
                base = c
        node = (
            {"ce": leg, "pe": None}
            if drv_option_type.upper() == "CALL"
            else {"ce": None, "pe": leg}
        )
        return {"data": node}

    # ---------------- market data ----------------
    async def get_expiry_list(self, sec_id: int, seg: str = "IDX_I") -> List[str]:
        data = await self._request(
            "POST",
            "/optionchain/expirylist",
            json={"UnderlyingScrip": sec_id, "UnderlyingSeg": seg},
        )
        return data.get("data", []) or []

    async def get_option_chain(
        self, sec_id: int, expiry: str, seg: str = "IDX_I"
    ) -> Dict[str, Any]:
        data = await self._request(
            "POST",
            "/optionchain",
            json={"UnderlyingScrip": sec_id, "UnderlyingSeg": seg, "Expiry": expiry},
        )
        return data.get("data", {}) or {}

    async def get_ltp(self, instruments: Dict[str, List[int]]) -> Dict[str, Any]:
        """Batch last-traded-price snapshot (up to 1000 instruments, 1 req/sec).

        `instruments` maps segment -> security ids, e.g.
            {"IDX_I": [13, 51], "NSE_FNO": [49081]}
        Response: {"data": {"IDX_I": {"13": {"last_price": 23100.5}}, ...}}
        """
        return await self._request("POST", "/marketfeed/ltp", json=instruments)

    # ---------------- historical / analytics data ----------------
    async def get_historical_daily(
        self,
        security_id: str,
        exchange_segment: str,
        instrument: str,
        from_date: str,
        to_date: str,
        *,
        oi: bool = False,
        expiry_code: int = 0,
    ) -> Dict[str, Any]:
        """Daily OHLC candles. `from_date`/`to_date` are 'YYYY-MM-DD'."""
        return await self._request(
            "POST",
            "/charts/historical",
            json={
                "securityId": str(security_id),
                "exchangeSegment": exchange_segment,
                "instrument": instrument,
                "expiryCode": expiry_code,
                "oi": oi,
                "fromDate": from_date,
                "toDate": to_date,
            },
        )

    async def get_intraday(
        self,
        security_id: str,
        exchange_segment: str,
        instrument: str,
        interval: str,
        from_dt: str,
        to_dt: str,
        *,
        oi: bool = False,
        throttle: bool = True,
    ) -> Dict[str, Any]:
        """Intraday OHLC candles. Dates 'YYYY-MM-DD HH:MM:SS'. Max 90 days/call."""
        return await self._request(
            "POST",
            "/charts/intraday",
            json={
                "securityId": str(security_id),
                "exchangeSegment": exchange_segment,
                "instrument": instrument,
                "interval": str(interval),
                "oi": oi,
                "fromDate": from_dt,
                "toDate": to_dt,
            },
            throttle=throttle,
        )

    async def get_rolling_expired_options(
        self,
        security_id: str,
        exchange_segment: str,
        instrument: str,
        interval: str,
        expiry_flag: str,
        expiry_code: int,
        strike: str,
        drv_option_type: str,
        required_data: List[str],
        from_date: str,
        to_date: str,
    ) -> Dict[str, Any]:
        """5-yr strike-wise EXPIRED option data. `strike` e.g. 'ATM','ATM+1'."""
        return await self._request(
            "POST",
            "/charts/rollingoption",
            json={
                "securityId": str(security_id),
                "exchangeSegment": exchange_segment,
                "instrument": instrument,
                "interval": str(interval),
                "expiryFlag": expiry_flag,
                "expiryCode": int(expiry_code),
                "strike": strike,
                "drvOptionType": drv_option_type,
                "requiredData": required_data,
                "fromDate": from_date,
                "toDate": to_date,
            },
        )

    # ---------------- account ----------------
    async def get_profile(self) -> Dict[str, Any]:
        return await self._request("GET", "/profile")

    async def get_fund_limit(self, *, fresh: bool = False) -> Dict[str, Any]:
        return await self._cached_read("/fundlimit", fresh=fresh)

    async def get_positions(self, *, fresh: bool = False) -> List[Dict[str, Any]]:
        return await self._cached_read("/positions", fresh=fresh)

    async def get_holdings(self) -> List[Dict[str, Any]]:
        return await self._request("GET", "/holdings")

    # ---------------- orders ----------------
    async def place_order(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Plain order (LIMIT/MARKET) — used for instant square-off/exit."""
        self._read_cache.clear()
        return await self._request("POST", "/orders", json=payload)

    async def place_super_order(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self._read_cache.clear()
        return await self._request("POST", "/super/orders", json=payload)

    async def cancel_super_order(self, order_id: str, leg: str) -> Dict[str, Any]:
        self._read_cache.clear()
        return await self._request("DELETE", f"/super/orders/{order_id}/{leg}")

    async def cancel_order(self, order_id: str) -> Dict[str, Any]:
        """Cancel a regular (non-super) order by id."""
        self._read_cache.clear()
        return await self._request("DELETE", f"/orders/{order_id}")

    async def get_super_orders(self, *, fresh: bool = False) -> List[Dict[str, Any]]:
        return await self._cached_read("/super/orders", fresh=fresh)

    async def get_order_book(self, *, fresh: bool = False) -> List[Dict[str, Any]]:
        return await self._cached_read("/orders", fresh=fresh)

    async def get_order_by_id(self, order_id: str) -> Dict[str, Any]:
        return await self._get_order_detail(f"/orders/{order_id}", order_id=order_id)

    async def get_order_by_correlation(self, correlation_id: str) -> Dict[str, Any]:
        return await self._get_order_detail(f"/orders/external/{correlation_id}", correlation=correlation_id)

    async def _get_order_detail(self, path: str, *, order_id: str = "", correlation: str = "") -> Dict[str, Any]:
        try:
            raw = await self._request("GET", path)
            return self._order_detail(raw, order_id=order_id, correlation=correlation)
        except DhanAPIError as exc:
            if exc.error_code != "UNKNOWN_ORDER" and exc.status_code != 404:
                raise
            logger.warning("account=%s detail unavailable; verifying exact identity against fresh current order book",
                           self.account_id)
            book = await self.get_order_book(fresh=True)
            return self._order_detail(book, order_id=order_id, correlation=correlation)

    @staticmethod
    def _order_detail(raw: Any, *, order_id: str = "", correlation: str = "") -> Dict[str, Any]:
        rows = raw if isinstance(raw, list) else [raw]
        matches = [row for row in rows if isinstance(row, dict) and row.get("orderStatus")
                   and ((order_id and str(row.get("orderId")) == order_id)
                        or (correlation and row.get("correlationId") == correlation))]
        if len(matches) != 1:
            raise DhanAPIError(0, "UNKNOWN_ORDER", "Broker order missing/ambiguous; outcome unknown. No automatic replay.")
        return matches[0]

    async def get_trades(self) -> List[Dict[str, Any]]:
        return await self._request("GET", "/trades")

    async def get_ip(self) -> Dict[str, Any]:
        result = await self._request("GET", "/ip/getIP")
        if not isinstance(result, dict):
            message = next((str(row["message"]) for row in result
                            if isinstance(row, dict) and row.get("message")), None) \
                if isinstance(result, list) else None
            reason = message or "Dhan returned an invalid static-IP response; cannot verify order permission."
            logger.warning("account=%s static_ip_unverified response_type=%s reason=%s",
                           self.account_id, type(result).__name__, reason)
            raise DhanAPIError(200, "IP_NOT_VERIFIED", reason)
        allowed = result.get("ordersAllowed")
        if (allowed is not None and not isinstance(allowed, bool)) or (
            allowed is None and not result.get("ipMatchStatus")
        ):
            reason = str(result.get("message") or
                         "Dhan returned no valid static-IP permission verdict; check IP registration for this account.")
            logger.warning("account=%s static_ip_unverified reason=%s", self.account_id, reason)
            raise DhanAPIError(200, "IP_NOT_VERIFIED", reason)
        return result

    async def set_ip(self, ip: str, flag: str) -> Dict[str, Any]:
        """Register the outbound IP with Dhan (required before live orders).

        `flag` is 'PRIMARY' or 'SECONDARY'.
        """
        return await self._request(
            "POST",
            "/ip/setIP",
            json={"dhanClientId": self._client_id, "ip": ip, "ipFlag": flag},
        )

    async def detect_public_ip(self) -> Optional[str]:
        """Best-effort lookup of THIS machine's public egress IP.

        Used to tell the user exactly which IP to whitelist for live orders.
        Never raises — returns None on any failure.
        """
        for url in ("https://api.ipify.org?format=json", "https://ifconfig.me/ip"):
            try:
                async with httpx.AsyncClient(timeout=6.0, transport=_transport(self.source_ip)) as c:
                    r = await c.get(url)
                    if r.status_code == 200:
                        text = r.text.strip()
                        if text.startswith("{"):
                            return r.json().get("ip")
                        return text
            except Exception:  # pragma: no cover - network best effort
                continue
        return None


_client: Optional[DhanClient] = None
_trading_clients: Dict[str, DhanClient] = {}


def get_trading_client() -> DhanClient:
    from .accounts import account_credentials, current_account
    account = current_account()
    if account not in _trading_clients:
        client_id, token = account_credentials(account)
        if not client_id or not token:
            raise DhanAPIError(0, "NOT_CONFIGURED", f"{account} trading credentials missing")
        _trading_clients[account] = DhanClient(
            client_id=client_id, access_token=token, account_id=account,
        )
    return _trading_clients[account]


def get_dhan_client() -> DhanClient:
    global _client
    if _client is None:
        _client = DhanClient()
    return _client


async def close_dhan_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
    for client in _trading_clients.values():
        await client.aclose()
    _trading_clients.clear()
