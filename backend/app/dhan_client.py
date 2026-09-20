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
import math
import random
import time
from typing import Any, Dict, List, Optional

import httpx

from .config import get_settings

logger = logging.getLogger("dhan.client")

BASE_URL = "https://api.dhan.co/v2"

MOCK_TOKEN_PREFIX = "mock"

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

    def __init__(self, timeout: float = 15.0) -> None:
        s = get_settings()
        self._client_id = s.dhan_client_id
        self._headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "access-token": s.dhan_access_token,
            "client-id": s.dhan_client_id,
        }
        self._client = httpx.AsyncClient(base_url=BASE_URL, timeout=timeout)
        self._mock = s.dhan_access_token.lower().startswith(MOCK_TOKEN_PREFIX)
        if self._mock:
            logger.warning("DhanClient running in MOCK mode (synthetic data, no network)")

    async def aclose(self) -> None:
        await self._client.aclose()

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
    ) -> Any:
        if self._mock:
            return self._mock_response(method, path, json or {})

        # Serialise heavy chart calls through one gate so parallel panels can't
        # burst the data API and trip Dhan's rate limit (DH-904 / 429).
        if any(p in path for p in _HEAVY_PATHS):
            await self._await_heavy_slot()

        try:
            resp = await self._client.request(
                method, path, headers=self._headers, json=json
            )
        except httpx.HTTPError as exc:  # network-level failure
            logger.warning("Network error calling %s %s: %s", method, path, exc)
            raise DhanAPIError(0, "DH-909", f"Network error: {exc}") from exc

        if resp.status_code >= 400:
            code, msg = self._extract_error(resp)
            logger.warning("Dhan %s %s -> %s [%s] %s", method, path, resp.status_code, code, msg)
            raise DhanAPIError(resp.status_code, code, msg)

        if resp.status_code == 202 or not resp.content:
            return {"status": "accepted"}
        try:
            return resp.json()
        except ValueError:
            return {"raw": resp.text}

    @staticmethod
    def _extract_error(resp: httpx.Response) -> tuple[str, str]:
        try:
            data = resp.json()
        except ValueError:
            return "DH-910", resp.text[:300]
        code = str(data.get("errorCode") or data.get("error_code") or "DH-910")
        msg = str(
            data.get("errorMessage")
            or data.get("error_message")
            or data.get("message")
            or "Unknown error"
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
            return self._mock_historical(90)
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

    async def get_fund_limit(self) -> Dict[str, Any]:
        return await self._request("GET", "/fundlimit")

    async def get_positions(self) -> List[Dict[str, Any]]:
        return await self._request("GET", "/positions")

    async def get_holdings(self) -> List[Dict[str, Any]]:
        return await self._request("GET", "/holdings")

    # ---------------- orders ----------------
    async def place_order(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Plain order (LIMIT/MARKET) — used for instant square-off/exit."""
        return await self._request("POST", "/orders", json=payload)

    async def place_super_order(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return await self._request("POST", "/super/orders", json=payload)

    async def cancel_super_order(self, order_id: str, leg: str) -> Dict[str, Any]:
        return await self._request("DELETE", f"/super/orders/{order_id}/{leg}")

    async def get_super_orders(self) -> List[Dict[str, Any]]:
        return await self._request("GET", "/super/orders")

    async def get_order_book(self) -> List[Dict[str, Any]]:
        return await self._request("GET", "/orders")

    async def get_trades(self) -> List[Dict[str, Any]]:
        return await self._request("GET", "/trades")

    async def get_ip(self) -> Dict[str, Any]:
        return await self._request("GET", "/ip/getIP")

    async def set_ip(self, ip: str, flag: str) -> Dict[str, Any]:
        """Register the outbound IP with Dhan (required before live orders).

        `flag` is 'PRIMARY' or 'SECONDARY'.
        """
        s = get_settings()
        return await self._request(
            "POST",
            "/ip/setIP",
            json={"dhanClientId": s.dhan_client_id, "ip": ip, "ipFlag": flag},
        )

    async def detect_public_ip(self) -> Optional[str]:
        """Best-effort lookup of THIS machine's public egress IP.

        Used to tell the user exactly which IP to whitelist for live orders.
        Never raises — returns None on any failure.
        """
        for url in ("https://api.ipify.org?format=json", "https://ifconfig.me/ip"):
            try:
                async with httpx.AsyncClient(timeout=6.0) as c:
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
