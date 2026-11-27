"""Raw market-microstructure recorder (order book + trade ticks) for future research.

Purpose: build our own history for order-book imbalance and signed-volume (CVD) studies, which
Dhan/NSE do not sell historically. Read-only market data (Master token; 200-level depth on the Child
token); never places orders and never touches the trading feed (it uses its own WebSocket connections).

Streams (Dhan docs: /docs/v2/live-market-feed, /docs/v2/full-market-depth):
  * ``feed``   - Full packets (RequestCode 21): LTP, LTQ, LTT, ATP, volume, total buy/sell qty, OI and
                 5-level depth for current index futures + near-expiry ATM options of NIFTY and SENSEX.
  * ``depth20``- 20-level depth (RequestCode 23, NSE only): NIFTY futures + NIFTY ATM options.
  * ``depth200``- 200-level depth (RequestCode 23 on the 200-depth socket, 1 instrument per socket):
                 NIFTY current future + NIFTY ATM CE + ATM PE, one socket each. Uses the Child token
                 (own Data plan) so Master stays under Dhan's 5-socket limit; without Child it falls
                 back to Master for the future only. ATM switches only after the new strike holds
                 ``DEPTH200_SWITCH_SECONDS``; every switch is logged to ``instruments.jsonl``.

Storage: ``<algo2 dir>/microstructure/<day>/<stream>.bin.gz``. Each flush appends one gzip member,
so the file stays a valid gzip stream. Each record is ``<d H`` (receive epoch seconds float64,
payload length uint16) followed by the raw WebSocket message bytes, exactly as received. A message
may hold several packets; split them with ``split_feed``/``split_depth``. ``instruments.jsonl``
logs every subscription (what each security id is) and ``events.jsonl`` logs connects/errors.

Nothing here is ever lost: the daily S3 archive (``app/archive.py``) keeps every day permanently
and only then frees old days from the small server disk. If free disk space drops below
``MIN_FREE_BYTES`` recording pauses
(trading keeps its disk headroom) and resumes once space is available again.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import logging
import os
import shutil
import struct
import time
from datetime import datetime
from typing import Any, Dict, Iterator, List, Optional, Tuple

from ..config import get_settings
from ..instruments import get_index
from ..optionchain import engine as chain_engine

logger = logging.getLogger("dhan.algo2.micro")

try:  # websockets >= 13
    from websockets.asyncio.client import connect as ws_connect
except Exception:  # pragma: no cover - older layouts
    from websockets.client import connect as ws_connect  # type: ignore

FEED_URL = "wss://api-feed.dhan.co?version=2&token={token}&clientId={client}&authType=2"
DEPTH_URL = "wss://depth-api-feed.dhan.co/twentydepth?token={token}&clientId={client}&authType=2"
DEPTH200_URL = "wss://full-depth-api.dhan.co/twohundreddepth?token={token}&clientId={client}&authType=2"
DEPTH200_SLOTS = ("fut", "ce", "pe")
DEPTH200_SWITCH_SECONDS = 60.0
REQ_FULL, REQ_DEPTH20 = 21, 23
START_MIN, STOP_MIN = 9 * 60 + 10, 15 * 60 + 35       # IST window, weekdays
FLUSH_SECONDS, FLUSH_BYTES = 5.0, 1 << 20
MIN_FREE_BYTES = 1 << 30                              # pause below 1 GiB free
ATM_EACH_SIDE = {"feed": 2, "depth20": 1}
DEPTH20_MAX = 50                                      # Dhan cap per 20-depth connection
BOOK_MAX_AGE = 60.0                                   # TBS older than this (s) counts as missing
REC = struct.Struct("<dH")
SEGMENT_ID = {"IDX_I": 0, "NSE_EQ": 1, "NSE_FNO": 2, "BSE_EQ": 4, "BSE_FNO": 8}

FULL = struct.Struct("<BHBi f h i f i i i i i i f f f f")   # 62-byte header+trade block of a full packet
LEVEL5 = struct.Struct("<i i h h f f")
LEVEL20 = struct.Struct("<d I I")
DEPTH_HDR = struct.Struct("<H B B i I")


# ------------------------------------------------------------------ decoding
def split_feed(msg: bytes) -> List[bytes]:
    """Split a live-feed message into packets (8-byte header, int16 length at bytes 1-2)."""
    out, i = [], 0
    while i + 8 <= len(msg):
        length = struct.unpack_from("<H", msg, i + 1)[0]
        if length < 8 or i + length > len(msg):
            out.append(msg[i:])
            break
        out.append(msg[i:i + length])
        i += length
    return out


def split_depth(msg: bytes) -> List[bytes]:
    """Split a 20-depth message into packets (12-byte header, int16 length at bytes 0-1)."""
    out, i = [], 0
    while i + 12 <= len(msg):
        length = struct.unpack_from("<H", msg, i)[0]
        if length < 12 or i + length > len(msg):
            out.append(msg[i:])
            break
        out.append(msg[i:i + length])
        i += length
    return out


def parse_full(pkt: bytes) -> Optional[Dict[str, Any]]:
    """Decode a Full packet (code 8, 162 bytes)."""
    if len(pkt) < FULL.size + 5 * LEVEL5.size or pkt[0] != 8:
        return None
    (code, _ln, seg, sid, ltp, ltq, ltt, atp, vol, sell_qty, buy_qty, oi, oi_hi, oi_lo,
     o, c, h, lo) = FULL.unpack_from(pkt, 0)
    depth = []
    for n in range(5):
        bq, aq, bo, ao, bp, ap = LEVEL5.unpack_from(pkt, FULL.size + n * LEVEL5.size)
        depth.append({"bidQty": bq, "askQty": aq, "bidOrders": bo, "askOrders": ao, "bid": bp, "ask": ap})
    return {"segment": seg, "securityId": sid, "ltp": ltp, "ltq": ltq, "ltt": ltt, "atp": atp, "volume": vol,
            "totalSellQty": sell_qty, "totalBuyQty": buy_qty, "oi": oi, "oiHigh": oi_hi, "oiLow": oi_lo,
            "open": o, "close": c, "high": h, "low": lo, "depth": depth}


def parse_depth20(pkt: bytes) -> Optional[Dict[str, Any]]:
    """Decode a 20-level depth packet (code 41 = bids, 51 = asks)."""
    if len(pkt) < DEPTH_HDR.size:
        return None
    _ln, code, seg, sid, _seq = DEPTH_HDR.unpack_from(pkt, 0)
    if code not in (41, 51):
        return None
    levels = []
    for off in range(DEPTH_HDR.size, len(pkt) - LEVEL20.size + 1, LEVEL20.size):
        price, qty, orders = LEVEL20.unpack_from(pkt, off)
        levels.append({"price": price, "qty": qty, "orders": orders})
    return {"side": "bid" if code == 41 else "ask", "segment": seg, "securityId": sid, "levels": levels}


def read_records(path: str) -> Iterator[Tuple[float, bytes]]:
    """Yield (receive_epoch, raw_message) from a recorded ``.bin.gz`` stream."""
    with gzip.open(path, "rb") as fh:
        data = fh.read()
    i = 0
    while i + REC.size <= len(data):
        at, length = REC.unpack_from(data, i)
        i += REC.size
        if i + length > len(data):
            break
        yield at, data[i:i + length]
        i += length


# --------------------------------------------------------------- selection
def atm_rows(rows: List[Dict[str, Any]], spot: Optional[float], each_side: int) -> List[Dict[str, Any]]:
    rows = [r for r in rows if r.get("strike") is not None]
    if not rows or not spot:
        return []
    rows.sort(key=lambda r: float(r["strike"]))
    atm = min(range(len(rows)), key=lambda i: abs(float(rows[i]["strike"]) - float(spot)))
    return rows[max(0, atm - each_side): atm + each_side + 1]


def option_targets(index_key: str, snap: Optional[Dict[str, Any]], each_side: int) -> List[Dict[str, Any]]:
    inst = get_index(index_key)
    if not inst or not snap:
        return []
    out = []
    for row in atm_rows(list(snap.get("rows") or []), snap.get("underlyingLtp"), each_side):
        for leg in ("ce", "pe"):
            sid = (row.get(leg) or {}).get("securityId")
            if sid:
                out.append({"segment": inst.derivative_segment, "securityId": int(sid), "index": index_key,
                            "kind": "option", "strike": float(row["strike"]), "type": leg.upper(),
                            "expiry": snap.get("expiry")})
    return out


def in_window(now: datetime) -> bool:
    minute = now.hour * 60 + now.minute
    return now.weekday() < 5 and START_MIN <= minute < STOP_MIN


# ------------------------------------------------------------------ writer
class StreamWriter:
    """Buffered append-only writer; one gzip member per flush. Never deletes or truncates."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.buf = bytearray()
        self.last_flush = time.monotonic()
        self.records = 0
        self.bytes_raw = 0

    def add(self, at: float, msg: bytes) -> None:
        if len(msg) > 0xFFFF:
            msg = msg[:0xFFFF]
        self.buf += REC.pack(at, len(msg)) + msg
        self.records += 1
        self.bytes_raw += len(msg)

    def due(self) -> bool:
        return bool(self.buf) and (len(self.buf) >= FLUSH_BYTES or time.monotonic() - self.last_flush >= FLUSH_SECONDS)

    def take(self) -> bytes:
        data, self.buf = bytes(self.buf), bytearray()
        self.last_flush = time.monotonic()
        return data

    def write(self, data: bytes) -> None:
        if not data:
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "ab") as fh:
            fh.write(gzip.compress(data, 6))


class MicrostructureRecorder:
    def __init__(self, directory: str, futures_source: Any = None) -> None:
        self.dir = directory
        self.futures_source = futures_source     # object with .futures {index: {securityId, segment, ...}}
        self.day: Optional[str] = None
        self.writers: Dict[str, StreamWriter] = {}
        self.known: Dict[str, Dict[Tuple[str, int], Dict[str, Any]]] = {"feed": {}, "depth20": {}}
        self.sent: Dict[str, set] = {"feed": set(), "depth20": set()}
        self.ws: Dict[str, Any] = {}
        self.connected: Dict[str, bool] = {"feed": False, "depth20": False}
        self.paused_for_disk = False
        self.errors: Dict[str, Optional[str]] = {"feed": None, "depth20": None}
        self.book: Dict[str, Tuple[float, int, int]] = {}   # index -> (at, totalBuyQty, totalSellQty) of its future
        self.d200: Dict[str, Optional[Dict[str, Any]]] = {k: None for k in DEPTH200_SLOTS}   # active target
        self.d200_book: Dict[int, Tuple[float, int]] = {}   # side code -> (at, sum of 200 quantities)
        self.d200_pending: Dict[str, Tuple[Optional[Tuple[str, int]], float]] = {}
        self.d200_connected: Dict[str, bool] = {k: False for k in DEPTH200_SLOTS}
        self.d200_ws: Dict[str, Any] = {}
        self.d200_errors: Dict[str, Optional[str]] = {k: None for k in DEPTH200_SLOTS}
        self._tasks: List[asyncio.Task] = []
        self._running = False
        self._next_futures_try = 0.0

    # -------------------------------------------------------- state
    def folder(self) -> str:
        return os.path.join(self.dir, self.day or "unknown")

    def disk_ok(self) -> bool:
        try:
            os.makedirs(self.dir, exist_ok=True)
            free = shutil.disk_usage(self.dir).free
        except OSError:
            return False
        ok = free >= MIN_FREE_BYTES
        if ok == self.paused_for_disk:
            self.paused_for_disk = not ok
            msg = ("Microstructure recording PAUSED: free disk %.2f GB < %.2f GB (nothing deleted)" if not ok
                   else "Microstructure recording resumed: free disk %.2f GB (min %.2f GB)")
            logger.warning(msg, free / 2 ** 30, MIN_FREE_BYTES / 2 ** 30)
            self.event({"event": "disk_pause" if not ok else "disk_resume", "freeBytes": free})
        return ok

    def event(self, row: Dict[str, Any]) -> None:
        self._append_jsonl("events.jsonl", {"at": time.time(), **row})

    def _append_jsonl(self, name: str, row: Dict[str, Any]) -> None:
        try:
            os.makedirs(self.folder(), exist_ok=True)
            with open(os.path.join(self.folder(), name), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, separators=(",", ":"), default=str) + "\n")
        except OSError:
            logger.warning("Microstructure %s write failed", name, exc_info=True)

    def roll_day(self, now: datetime) -> None:
        day = now.date().isoformat()
        if day == self.day:
            return
        self.flush_all(force=True)
        self.day = day
        self.writers = {s: StreamWriter(os.path.join(self.folder(), f"{s}.bin.gz"))
                        for s in ("feed", "depth20", "depth200")}
        self.known = {"feed": {}, "depth20": {}}
        self.sent = {"feed": set(), "depth20": set()}
        self.d200 = {k: None for k in DEPTH200_SLOTS}
        self.d200_book.clear()
        self.d200_pending = {}

    def note_book(self, msg: bytes, at: Optional[float] = None) -> None:
        """Keep the latest total buy/sell quantity of each recorded index future (live TBS gate)."""
        futs = {int(r["securityId"]): r["index"] for r in self.known.get("feed", {}).values()
                if r.get("kind") == "future"}
        if not futs:
            return
        for pkt in split_feed(msg):
            if len(pkt) < FULL.size or pkt[0] != 8:
                continue
            sid = struct.unpack_from("<i", pkt, 4)[0]
            if sid in futs:
                fields = FULL.unpack_from(pkt, 0)
                self.book[futs[sid]] = (time.time() if at is None else at, fields[10], fields[9])

    def tbs(self, index_key: str, max_age: float = BOOK_MAX_AGE) -> Optional[float]:
        """(totalBuyQty - totalSellQty) / sum for the index future, None when missing or stale."""
        row = self.book.get(index_key)
        if not row or time.time() - row[0] > max_age or row[1] + row[2] <= 0:
            return None
        return (row[1] - row[2]) / (row[1] + row[2])

    def note_depth200(self, msg: bytes, at: Optional[float] = None) -> None:
        """Cache full bid and ask quantities for the subscribed NIFTY future."""
        target = self.d200.get("fut")
        if not target:
            return
        packets = split_depth(msg)
        if sum(map(len, packets)) != len(msg):
            self.d200_book.clear()
            logger.warning("Incomplete 200-level future depth message; entry gate blocked")
            return
        updates: Dict[int, Tuple[float, int]] = {}
        timestamp = time.time() if at is None else at
        for pkt in packets:
            if len(pkt) < DEPTH_HDR.size:
                self.d200_book.clear()
                logger.warning("Incomplete 200-level future depth packet; entry gate blocked")
                return
            length, code, segment, sid, count = DEPTH_HDR.unpack_from(pkt)
            if code not in (41, 51):
                continue
            if (sid != int(target["securityId"]) or segment != SEGMENT_ID["NSE_FNO"]
                    or length != len(pkt) or count != 200 or length != DEPTH_HDR.size + 200 * LEVEL20.size):
                self.d200_book.clear()
                logger.warning("Invalid 200-level future depth packet (side=%s, sid=%s, rows=%s); entry gate blocked",
                               code, sid, count)
                return
            book = parse_depth20(pkt)
            if book is None:
                self.d200_book.clear()
                logger.warning("Cannot decode 200-level future depth packet; entry gate blocked")
                return
            updates[code] = (timestamp, sum(level["qty"] for level in book["levels"]))
        self.d200_book.update(updates)

    def depth200_imbalance(self, index_key: str, max_age: float = BOOK_MAX_AGE) -> Optional[float]:
        """Full 200-level futures bid/ask imbalance, or None when either side is unusable."""
        target = self.d200.get("fut")
        current = (getattr(self.futures_source, "futures", None) or {}).get(index_key)
        if (index_key != "NIFTY" or not target or not current or not self.d200_connected["fut"]
                or target.get("securityId") != current.get("securityId")
                or target.get("segment") != current.get("segment")):
            return None
        bid, ask = self.d200_book.get(41), self.d200_book.get(51)
        now = time.time()
        if not bid or not ask or not 0 <= now - bid[0] <= max_age or not 0 <= now - ask[0] <= max_age:
            return None
        total = bid[1] + ask[1]
        return (bid[1] - ask[1]) / total if total > 0 else None

    def record(self, stream: str, msg: bytes, at: Optional[float] = None) -> None:
        writer = self.writers.get(stream)
        if writer is None or self.paused_for_disk:
            return
        writer.add(time.time() if at is None else at, msg)

    def flush_all(self, force: bool = False) -> None:
        for writer in self.writers.values():
            if writer.buf and (force or writer.due()):
                try:
                    writer.write(writer.take())
                except OSError:
                    logger.warning("Microstructure flush failed for %s", writer.path, exc_info=True)

    # ------------------------------------------------------ targets
    def targets(self) -> Dict[str, List[Dict[str, Any]]]:
        feed: List[Dict[str, Any]] = []
        depth: List[Dict[str, Any]] = []
        futures = getattr(self.futures_source, "futures", None) or {}
        for key in ("NIFTY", "SENSEX"):
            inst = get_index(key)
            if inst:
                feed.append({"segment": "IDX_I", "securityId": inst.sec_id, "index": key, "kind": "index"})
            fut = futures.get(key)
            if fut and fut.get("securityId"):
                row = {"segment": fut["segment"], "securityId": int(fut["securityId"]), "index": key,
                       "kind": "future", "expiry": fut.get("expiry")}
                feed.append(row)
                if fut["segment"] == "NSE_FNO":
                    depth.append(row)
            snap = chain_engine.snapshot(key)
            feed += option_targets(key, snap, ATM_EACH_SIDE["feed"])
            if inst and inst.derivative_segment == "NSE_FNO":
                depth += option_targets(key, snap, ATM_EACH_SIDE["depth20"])
        return {"feed": feed, "depth20": depth}

    def new_targets(self, stream: str, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        fresh = []
        for row in rows:
            ident = (row["segment"], int(row["securityId"]))
            if ident in self.known[stream]:
                continue
            if stream == "depth20" and len(self.known[stream]) >= DEPTH20_MAX:
                break
            self.known[stream][ident] = row
            self._append_jsonl("instruments.jsonl", {"at": time.time(), "stream": stream, **row})
            fresh.append(row)
        return fresh

    @staticmethod
    def subscribe_messages(stream: str, rows: List[Dict[str, Any]]) -> List[str]:
        if stream == "depth200":
            return [json.dumps({"RequestCode": REQ_DEPTH20, "ExchangeSegment": r["segment"],
                                "SecurityId": str(r["securityId"])}) for r in rows]
        code = REQ_FULL if stream == "feed" else REQ_DEPTH20
        size = 100 if stream == "feed" else DEPTH20_MAX
        msgs = []
        for i in range(0, len(rows), size):
            chunk = rows[i:i + size]
            msgs.append(json.dumps({"RequestCode": code, "InstrumentCount": len(chunk),
                                    "InstrumentList": [{"ExchangeSegment": r["segment"],
                                                        "SecurityId": str(r["securityId"])} for r in chunk]}))
        return msgs

    # ------------------------------------------------- 200-level depth
    @staticmethod
    def depth200_credentials() -> Tuple[str, str, bool]:
        """(client, token, is_child). Child keeps Master under Dhan's 5-socket-per-user limit."""
        s = get_settings()
        client, token = (s.dhan_child_client_id or "").strip(), (s.dhan_child_access_token or "").strip()
        if client and token and not token.lower().startswith("mock"):
            return client, token, True
        return s.dhan_client_id, s.dhan_access_token or "", False

    def depth200_wanted(self) -> Dict[str, Optional[Dict[str, Any]]]:
        wanted: Dict[str, Optional[Dict[str, Any]]] = {k: None for k in DEPTH200_SLOTS}
        fut = (getattr(self.futures_source, "futures", None) or {}).get("NIFTY")
        if fut and fut.get("securityId") and fut.get("segment") == "NSE_FNO":
            wanted["fut"] = {"segment": "NSE_FNO", "securityId": int(fut["securityId"]), "index": "NIFTY",
                             "kind": "future", "expiry": fut.get("expiry")}
        if self.depth200_credentials()[2]:
            for row in option_targets("NIFTY", chain_engine.snapshot("NIFTY"), 0):
                wanted[row["type"].lower()] = row
        return wanted

    def update_depth200(self, now_ts: Optional[float] = None) -> List[str]:
        """Apply wanted 200-depth targets; a changed strike must hold DEPTH200_SWITCH_SECONDS first.

        Returns the slots whose target changed (their socket must resubscribe)."""
        now_ts = time.time() if now_ts is None else now_ts
        changed = []
        for slot, row in self.depth200_wanted().items():
            cur = self.d200.get(slot)
            ident = (row["segment"], int(row["securityId"])) if row else None
            cur_ident = (cur["segment"], int(cur["securityId"])) if cur else None
            if ident == cur_ident or row is None:
                self.d200_pending.pop(slot, None)
                continue
            if cur is not None:
                pend = self.d200_pending.get(slot)
                if not pend or pend[0] != ident:
                    self.d200_pending[slot] = (ident, now_ts)
                    continue
                if now_ts - pend[1] < DEPTH200_SWITCH_SECONDS:
                    continue
            self.d200_pending.pop(slot, None)
            self.d200[slot] = row
            if slot == "fut":
                self.d200_book.clear()
            self._append_jsonl("instruments.jsonl", {"at": now_ts, "stream": "depth200", "slot": slot, **row})
            changed.append(slot)
        return changed

    # ----------------------------------------------------- lifecycle
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._tasks = [asyncio.create_task(self._supervise(), name="micro-supervisor"),
                       asyncio.create_task(self._connection("feed"), name="micro-feed"),
                       asyncio.create_task(self._connection("depth20"), name="micro-depth20")]
        self._tasks += [asyncio.create_task(self._depth200(slot), name=f"micro-depth200-{slot}")
                        for slot in DEPTH200_SLOTS]
        logger.info("Microstructure recorder started (%s)", self.dir)

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks = []
        await asyncio.to_thread(self.flush_all, True)

    def active(self, now: datetime) -> bool:
        s = get_settings()
        token = s.dhan_access_token or ""
        return bool(token) and not token.lower().startswith("mock") and in_window(now) and not self.paused_for_disk

    async def _supervise(self) -> None:
        from ..algo import now_ist
        while self._running:
            try:
                now = now_ist()
                if in_window(now):
                    self.roll_day(now)
                    self.disk_ok()
                    await self._ensure_futures(now)
                    targets = self.targets()
                    for stream, rows in targets.items():
                        fresh = self.new_targets(stream, rows)
                        ws = self.ws.get(stream)
                        if fresh and ws is not None:
                            for msg in self.subscribe_messages(stream, fresh):
                                await ws.send(msg)
                            self.sent[stream].update((r["segment"], r["securityId"]) for r in fresh)
                    for slot in self.update_depth200():
                        ws = self.d200_ws.get(slot)
                        if ws is not None:
                            await ws.close()
                due = [w for w in self.writers.values() if w.due()]
                for writer in due:
                    await asyncio.to_thread(writer.write, writer.take())
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - research recorder must never disturb trading
                logger.warning("Microstructure supervisor error", exc_info=True)
            await asyncio.sleep(1.0)

    async def _ensure_futures(self, now: datetime) -> None:
        """Resolve current futures via the shared Algo 2 cache, at most every 5 minutes on failure."""
        source = self.futures_source
        if source is None or getattr(source, "futures", None) or time.time() < self._next_futures_try:
            return
        self._next_futures_try = time.time() + 300
        await source.ensure_futures(now.date())

    async def _connection(self, stream: str) -> None:
        from ..algo import now_ist
        delay = 2.0
        while self._running:
            if not self.active(now_ist()) or not self.known[stream]:
                await asyncio.sleep(5.0)
                continue
            s = get_settings()
            url = (FEED_URL if stream == "feed" else DEPTH_URL).format(token=s.dhan_access_token, client=s.dhan_client_id)
            try:
                async with ws_connect(url, ping_interval=15, ping_timeout=20, max_size=2 ** 22) as ws:
                    self.ws[stream], self.connected[stream], self.errors[stream] = ws, True, None
                    rows = list(self.known[stream].values())
                    for msg in self.subscribe_messages(stream, rows):
                        await ws.send(msg)
                    self.sent[stream] = {(r["segment"], r["securityId"]) for r in rows}
                    self.event({"event": "connected", "stream": stream, "instruments": len(rows)})
                    logger.info("Microstructure %s stream connected (%d instruments)", stream, len(rows))
                    delay = 2.0
                    async for message in ws:
                        if isinstance(message, (bytes, bytearray)):
                            self.record(stream, bytes(message))
                            if stream == "feed":
                                try:
                                    self.note_book(bytes(message))
                                except Exception:  # noqa: BLE001 - must never break the recorder
                                    logger.debug("TBS parse failed", exc_info=True)
                        if not self.active(now_ist()):
                            break
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.errors[stream] = f"{type(exc).__name__}: {exc}"[:300]
                self.event({"event": "error", "stream": stream, "error": self.errors[stream]})
                logger.warning("Microstructure %s stream error (%s); retry in %.0fs", stream, exc, delay)
            finally:
                self.ws.pop(stream, None)
                self.connected[stream] = False
            await asyncio.sleep(delay)
            delay = min(delay * 2, 120.0)

    async def _depth200(self, slot: str) -> None:
        """One 200-level socket per slot; reconnects when the slot's (debounced) target changes."""
        from ..algo import now_ist
        delay = 2.0
        while self._running:
            row = self.d200.get(slot)
            if row is None or not self.active(now_ist()):
                await asyncio.sleep(5.0)
                continue
            client, token, is_child = self.depth200_credentials()
            try:
                async with ws_connect(DEPTH200_URL.format(token=token, client=client),
                                      ping_interval=15, ping_timeout=20, max_size=2 ** 22) as ws:
                    self.d200_ws[slot] = ws
                    await ws.send(self.subscribe_messages("depth200", [row])[0])
                    self.d200_connected[slot], self.d200_errors[slot] = True, None
                    self.event({"event": "connected", "stream": "depth200", "slot": slot,
                                "securityId": row["securityId"], "account": "child" if is_child else "master"})
                    logger.info("Microstructure depth200 %s connected (%s)", slot, row["securityId"])
                    async for message in ws:
                        if isinstance(message, (bytes, bytearray)):
                            raw, at = bytes(message), time.time()
                            self.record("depth200", raw, at=at)
                            if slot == "fut" and self.d200.get(slot) is row:
                                self.note_depth200(raw, at=at)
                            delay = 2.0
                        if self.d200.get(slot) is not row or not self.active(now_ist()):
                            break
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - research recorder must never disturb trading
                self.d200_errors[slot] = f"{type(exc).__name__}: {exc}"[:300]
                self.event({"event": "error", "stream": "depth200", "slot": slot, "error": self.d200_errors[slot]})
                logger.warning("Microstructure depth200 %s error (%s); retry in %.0fs", slot, exc, delay)
            finally:
                self.d200_ws.pop(slot, None)
                self.d200_connected[slot] = False
                if slot == "fut":
                    self.d200_book.clear()
            if self.d200.get(slot) is row:          # same target: dropped/closed by Dhan -> back off
                await asyncio.sleep(delay)
                delay = min(delay * 2, 120.0)

    def status(self) -> Dict[str, Any]:
        return {
            "day": self.day,
            "pausedForDisk": self.paused_for_disk,
            "connected": dict(self.connected),
            "errors": dict(self.errors),
            "instruments": {s: len(v) for s, v in self.known.items()},
            "records": {s: w.records for s, w in self.writers.items()},
            "rawBytes": {s: w.bytes_raw for s, w in self.writers.items()},
            "depth200": {slot: {"securityId": (row or {}).get("securityId"), "strike": (row or {}).get("strike"),
                                "connected": self.d200_connected.get(slot, False),
                                "error": self.d200_errors.get(slot)} for slot, row in self.d200.items()},
        }
