"""Live market data feed — one interface, two transports.

  * WebSocket  (wss://api-feed.dhan.co)  -> tick-by-tick. This is the fast path
    the algo uses to spot an opening-range break in ~10-30ms.
  * REST /marketfeed/ltp (1 request/second) -> snapshot poller, used
    automatically whenever the socket is down, still connecting, or silent.

Both write into a single {security_id: ltp} map, so the strategy code never
knows (or cares) which transport is live.

The socket is fed Dhan's binary, little-endian packets:
    byte 0      response code (2=ticker, 4=quote, 5=OI, 6=prev-close, 8=full)
    bytes 1-2   int16  message length
    byte  3     exchange segment id
    bytes 4-7   int32  security id
    bytes 8-11  float32 LTP        (ticker/quote/full)

Everything is best-effort and never raises into the caller: on any problem we
log, drop back to REST, and keep going.
"""
from __future__ import annotations

import asyncio
import logging
import math
import struct
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from .config import get_settings
from .dhan_client import DhanAPIError, get_dhan_client

logger = logging.getLogger("dhan.feed")


async def attach_ltp(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Quote prices separately from broker holdings/order-state authority."""
    instruments: Dict[str, List[int]] = {}
    for row in rows:
        segment = str(row.get("exchangeSegment") or "")
        sid = str(row.get("securityId") or "")
        if segment in SEGMENT_NAME_BY_ID.values() and sid.isdigit() and int(sid) > 0:
            ids = instruments.setdefault(segment, [])
            if int(sid) not in ids:
                ids.append(int(sid))
    if not instruments:
        return rows
    try:
        pairs = [(segment, sid) for segment, ids in instruments.items() for sid in ids]
        data: Dict[str, Any] = {}
        for offset in range(0, len(pairs), 1000):
            batch: Dict[str, List[int]] = {}
            for segment, sid in pairs[offset:offset + 1000]:
                batch.setdefault(segment, []).append(sid)
            response = await get_dhan_client().get_ltp(batch)
            if not isinstance(response, dict) or not isinstance(response.get("data"), dict):
                raise DhanAPIError(0, "BAD_QUOTES", "Broker returned invalid quote data")
            for segment, nodes in response["data"].items():
                if not isinstance(nodes, dict):
                    raise DhanAPIError(0, "BAD_QUOTES", "Broker returned invalid segment quotes")
                data.setdefault(segment, {}).update(nodes)
    except DhanAPIError as exc:
        logger.warning("Portfolio quote sync failed: %s", exc)
        for row in rows:
            row["quoteError"] = exc.error_message
            row["ltp"] = None
        return rows
    for row in rows:
        node = (data.get(str(row.get("exchangeSegment"))) or {}).get(str(row.get("securityId"))) or {}
        raw = node.get("last_price")
        try:
            price = float(raw) if raw is not None else 0
        except (ValueError, TypeError):
            price = 0
        row["ltp"] = price if math.isfinite(price) and price > 0 else None
        row["quoteError"] = None if row["ltp"] is not None else "Price unavailable"
    return rows

try:  # websockets >= 13 exposes the asyncio client here
    from websockets.asyncio.client import connect as ws_connect
except Exception:  # pragma: no cover - older layouts
    from websockets.client import connect as ws_connect  # type: ignore

FEED_URL = "wss://api-feed.dhan.co"
REST_POLL_SECONDS = 1.0        # /marketfeed/ltp allows 1 request/second
WS_SILENCE_SECONDS = 20.0      # if the socket goes quiet this long, use REST too
MAX_RECONNECT_DELAY = 30.0

# Response codes
CODE_TICKER = 2
CODE_QUOTE = 4
CODE_OI = 5
CODE_PREV_CLOSE = 6
CODE_FULL = 8
CODE_DISCONNECT = 50
CODE_ERROR = 2005

SEGMENT_NAME_BY_ID = {
    0: "IDX_I",
    1: "NSE_EQ",
    2: "NSE_FNO",
    3: "NSE_CURRENCY",
    4: "BSE_EQ",
    5: "MCX_COMM",
    7: "BSE_CURRENCY",
    8: "BSE_FNO",
}


def parse_packet(pkt: bytes) -> Optional[Tuple[str, int, float]]:
    """Decode one feed packet -> (segment, security_id, ltp) or None."""
    if len(pkt) < 12:
        return None
    code = pkt[0]
    if code not in (CODE_TICKER, CODE_QUOTE, CODE_FULL):
        return None
    seg = SEGMENT_NAME_BY_ID.get(pkt[3])
    if seg is None:
        return None
    security_id = struct.unpack_from("<i", pkt, 4)[0]
    try:
        ltp = float(struct.unpack_from("<f", pkt, 8)[0])
    except struct.error:
        return None
    if not math.isfinite(ltp) or ltp <= 0:
        return None
    return seg, int(security_id), ltp


class MarketFeed:
    """Shared live-price cache with an automatic WS -> REST fallback."""

    def __init__(self) -> None:
        self._prices: Dict[tuple[str, int], float] = {}
        self._updated_at: Dict[tuple[str, int], float] = {}
        self._segments: Set[tuple[str, int]] = set()
        self._on_tick: Optional[Callable[[int, float, str], None]] = None
        self._listeners: List[Callable[[int, float, str], None]] = []

        self._ws_task: Optional[asyncio.Task] = None
        self._rest_task: Optional[asyncio.Task] = None
        self._running = False
        self._ws: Any = None

        self._connected = False
        self._mode = "off"                        # websocket | rest | off
        self._last_tick_at = 0.0
        self._last_ws_msg_at = 0.0
        self._tick_count = 0
        self._error: Optional[str] = None
        self._reconnects = 0
        self._lock = asyncio.Lock()

    # ---------------- public API ----------------
    def set_on_tick(self, cb: Optional[Callable[[int, float, str], None]]) -> None:
        self._on_tick = cb

    def add_listener(self, cb: Callable[[int, float, str], None]) -> None:
        """Extra tick consumer (e.g. Algo 2) alongside the primary on_tick callback."""
        if cb not in self._listeners:
            self._listeners.append(cb)

    def remove_listener(self, cb: Callable[[int, float, str], None]) -> None:
        if cb in self._listeners:
            self._listeners.remove(cb)

    def subscribe(self, security_id: int, segment: str) -> None:
        sid = int(security_id)
        key = (segment, sid)
        if key in self._segments:
            return
        self._segments.add(key)
        self._prices.setdefault(key, 0.0)
        if self._ws is not None:
            asyncio.ensure_future(self._push_subscription())

    def unsubscribe(self, security_id: int, segment: Optional[str] = None) -> None:
        sid = int(security_id)
        for key in list(self._segments):
            if key[1] == sid and (segment is None or key[0] == segment):
                self._segments.discard(key)
                self._prices.pop(key, None)
                self._updated_at.pop(key, None)

    def clear(self, keep: Set[int] | None = None) -> None:
        keep = keep or set()
        for segment, sid in list(self._segments):
            if sid not in keep:
                self.unsubscribe(sid, segment)

    def _key(self, security_id: int, segment: Optional[str]) -> Optional[tuple[str, int]]:
        if segment is not None:
            return segment, int(security_id)
        keys = [key for key in self._prices if key[1] == int(security_id)]
        return keys[0] if len(keys) == 1 else None

    def price(self, security_id: int, segment: Optional[str] = None) -> Optional[float]:
        key = self._key(security_id, segment)
        v = self._prices.get(key) if key is not None else None
        return v if v and v > 0 else None

    def age(self, security_id: int, segment: Optional[str] = None) -> Optional[float]:
        key = self._key(security_id, segment)
        stamp = self._updated_at.get(key) if key is not None else None
        return max(0.0, time.time() - stamp) if stamp is not None else None

    def subscribed(self) -> List[Dict[str, Any]]:
        return [
            {"securityId": sid, "segment": seg, "ltp": self._prices.get((seg, sid))}
            for seg, sid in sorted(self._segments)
        ]

    def status(self) -> Dict[str, Any]:
        return {
            "mode": self._mode,
            "connected": self._ws_healthy(),
            "webSocket": self._ws_healthy(),
            "ticks": self._tick_count,
            "reconnects": self._reconnects,
            "lastTickSecondsAgo": (
                round(time.time() - self._last_tick_at, 1) if self._last_tick_at else None
            ),
            "instruments": len(self._segments),
            "error": self._error,
        }

    # ---------------- lifecycle ----------------
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._ws_task = asyncio.create_task(self._ws_loop(), name="feed-ws")
        self._rest_task = asyncio.create_task(self._rest_loop(), name="feed-rest")
        logger.info("Market feed started (WebSocket + REST fallback)")

    async def stop(self) -> None:
        self._running = False
        for task in (self._ws_task, self._rest_task):
            if task:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        self._ws_task = self._rest_task = None
        self._ws = None
        self._connected = False
        self._mode = "off"
        logger.info("Market feed stopped")

    # ---------------- internals ----------------
    def _apply(self, security_id: int, ltp: float, segment: str = "IDX_I") -> None:
        if not math.isfinite(ltp) or ltp <= 0:
            return
        key = (segment, security_id)
        self._prices[key] = ltp
        self._updated_at[key] = time.time()
        self._tick_count += 1
        self._last_tick_at = time.time()
        for cb in ([self._on_tick] if self._on_tick is not None else []) + list(self._listeners):
            try:
                cb(security_id, ltp, segment)
            except Exception:  # noqa: BLE001 - never let a consumer break the feed
                logger.exception("feed on_tick callback failed")

    def _handle_packet(self, pkt: bytes) -> None:
        if not pkt:
            return
        if pkt[0] == CODE_DISCONNECT or pkt[0] == CODE_ERROR:
            logger.warning("Feed sent disconnect/error packet (code=%s)", pkt[0])
            self._connected = False
            return
        length = struct.unpack_from("<H", pkt, 1)[0] if len(pkt) >= 3 else 0
        if 8 <= length < len(pkt):
            self._handle_packet(pkt[:length])
            self._handle_packet(pkt[length:])
            return
        parsed = parse_packet(pkt)
        if parsed is None:
            return
        segment, sid, ltp = parsed
        self._last_ws_msg_at = time.time()
        self._apply(sid, ltp, segment)

    def _grouped(self, keys: Optional[Set[tuple[str, int]]] = None) -> Dict[str, List[int]]:
        out: Dict[str, List[int]] = {}
        for seg, sid in sorted(self._segments):
            if keys is not None and (seg, sid) not in keys:
                continue
            out.setdefault(seg, []).append(sid)
        return out

    async def _push_subscription(self) -> None:
        ws = self._ws
        if ws is None:
            return
        groups = self._grouped()
        if not groups:
            return
        instruments = [
            {"ExchangeSegment": seg, "SecurityId": str(sid)}
            for seg, ids in groups.items()
            for sid in ids
        ]
        # Dhan allows 100 instruments per subscribe message.
        for start in range(0, len(instruments), 100):
            chunk = instruments[start:start + 100]
            msg = {
                "RequestCode": 15,  # Ticker
                "InstrumentCount": len(chunk),
                "InstrumentList": chunk,
            }
            try:
                await ws.send(_json(msg))
            except Exception as exc:  # noqa: BLE001
                logger.warning("feed subscribe failed: %s", exc)
                return

    async def _ws_loop(self) -> None:
        s = get_settings()
        delay = 1.0
        while self._running:
            if not s.dhan_access_token or s.dhan_access_token.lower().startswith("mock"):
                await asyncio.sleep(5)
                continue
            url = (
                f"{FEED_URL}?version=2&token={s.dhan_access_token}"
                f"&clientId={s.dhan_client_id}&authType=2"
            )
            try:
                async with ws_connect(url, ping_interval=15, ping_timeout=20, max_size=2 ** 21) as ws:
                    self._ws = ws
                    self._connected = True
                    self._mode = "websocket"
                    self._error = None
                    self._last_ws_msg_at = time.time()
                    logger.info("Dhan market feed connected", extra={"telegram_event": "feed_connected"})
                    await self._push_subscription()
                    async for message in ws:
                        if not self._running:
                            break
                        if isinstance(message, (bytes, bytearray)):
                            initial_ticks = self._tick_count
                            self._handle_packet(bytes(message))
                            if self._tick_count > initial_ticks:
                                delay = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - network is best effort
                self._error = f"{type(exc).__name__}: {exc}"
                logger.warning("Dhan feed error (%s); retrying in %.0fs", exc, delay)
            finally:
                self._ws = None
                self._connected = False
                if self._mode == "websocket":
                    self._mode = "rest" if self._segments else "off"
            if not self._running:
                break
            self._reconnects += 1
            await asyncio.sleep(delay)
            delay = min(delay * 2, MAX_RECONNECT_DELAY)

    def _ws_healthy(self) -> bool:
        return self._connected and (time.time() - self._last_ws_msg_at) < WS_SILENCE_SECONDS

    async def _rest_loop(self) -> None:
        client = get_dhan_client()
        while self._running:
            try:
                if not self._segments:
                    await asyncio.sleep(REST_POLL_SECONDS)
                    continue
                stale: Set[tuple[str, int]] = set()
                for key in self._segments:
                    age = self.age(key[1], key[0])
                    if age is None or age > 5:
                        stale.add(key)
                if self._ws_healthy() and not stale:
                    await asyncio.sleep(REST_POLL_SECONDS)
                    continue
                if self._mode != "websocket":
                    self._mode = "rest"
                payload = self._grouped(stale if self._ws_healthy() else None)
                if not payload:
                    await asyncio.sleep(REST_POLL_SECONDS)
                    continue
                data = await client.get_ltp(payload)
                for seg, rows in (data.get("data") or {}).items():
                    if not isinstance(rows, dict):
                        continue
                    for sid_str, node in rows.items():
                        try:
                            sid = int(sid_str)
                            ltp = float((node or {}).get("last_price") or 0.0)
                        except (TypeError, ValueError):
                            continue
                        if ltp > 0:
                            self._apply(sid, ltp, seg)
                self._error = None
            except DhanAPIError as exc:
                self._error = exc.error_message
                logger.warning("Feed REST refresh failed: %s", exc)
                if exc.status_code == 401:
                    await asyncio.sleep(60)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._error = str(exc)
                logger.exception("Market feed REST processing failed")
            await asyncio.sleep(REST_POLL_SECONDS)


def _json(obj: Any) -> str:
    import json

    return json.dumps(obj)


feed = MarketFeed()
