"""Background option-chain cache ("polling engine").

Why a poller?
-------------
Dhan's FREE tier allows only 1 option-chain request per 3 seconds **per
underlying**. If the UI called Dhan directly on every render/tab-switch it
would (a) blow the rate limit and (b) feel slow.

So we keep a warm in-memory snapshot per index:
  * a background task refreshes each selected index on a stagger,
  * API endpoints return the cached snapshot instantly (never blocking),
  * the frontend polls OUR cache at its own pace (e.g. every 1-3s).

Result: instant UI, zero rate-limit violations, no hangs.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .config import get_settings
from .dhan_client import DhanAPIError, get_dhan_client
from .instruments import INDEX_REGISTRY, get_index

logger = logging.getLogger("dhan.optionchain")


@dataclass
class ChainSnapshot:
    index_key: str
    expiry: str
    underlying_ltp: float
    rows: List[Dict[str, Any]]
    fetched_at: float = field(default_factory=time.time)
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index_key,
            "expiry": self.expiry,
            "underlyingLtp": self.underlying_ltp,
            "rows": self.rows,
            "fetchedAt": self.fetched_at,
            "ageSeconds": round(time.time() - self.fetched_at, 2),
            "stale": (time.time() - self.fetched_at) > 12,
            "error": self.error,
        }


def _transform_chain(index_key: str, expiry: str, raw: Dict[str, Any]) -> ChainSnapshot:
    """Flatten Dhan's {strike: {ce, pe}} into a sorted row list."""
    oc = raw.get("oc", {}) or {}
    last_price = float(raw.get("last_price", 0.0) or 0.0)

    rows: List[Dict[str, Any]] = []
    for strike_str, legs in oc.items():
        try:
            strike = float(strike_str)
        except (TypeError, ValueError):
            continue
        ce = legs.get("ce", {}) or {}
        pe = legs.get("pe", {}) or {}
        rows.append(
            {
                "strike": strike,
                "ce": _flatten_leg(ce),
                "pe": _flatten_leg(pe),
            }
        )

    rows.sort(key=lambda r: r["strike"])
    return ChainSnapshot(index_key, expiry, last_price, rows)


def _flatten_leg(leg: Dict[str, Any]) -> Dict[str, Any]:
    greeks = leg.get("greeks", {}) or {}
    return {
        "securityId": leg.get("security_id"),
        "ltp": leg.get("last_price"),
        "prevClose": leg.get("previous_close_price"),
        "oi": leg.get("oi"),
        "prevOi": leg.get("previous_oi"),
        "volume": leg.get("volume"),
        "prevVolume": leg.get("previous_volume"),
        "avgPrice": leg.get("average_price"),
        "iv": leg.get("implied_volatility"),
        "bid": leg.get("top_bid_price"),
        "bidQty": leg.get("top_bid_quantity"),
        "ask": leg.get("top_ask_price"),
        "askQty": leg.get("top_ask_quantity"),
        "greeks": {
            "delta": greeks.get("delta"),
            "theta": greeks.get("theta"),
            "gamma": greeks.get("gamma"),
            "vega": greeks.get("vega"),
        },
    }


class OptionChainEngine:
    def __init__(self) -> None:
        self._snapshots: Dict[str, ChainSnapshot] = {}
        self._expiries: Dict[str, List[str]] = {}
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._expiry_override: Dict[str, str] = {}  # index_key -> chosen expiry
        # Per-index cooldown so rapid index-switching / on-demand warmups can
        # never burst Dhan and trigger a 429 (free tier = 1 req / 3s / underlying).
        self._last_fetch: Dict[str, float] = {}
        self._min_gap = 3.5

    # ---------- public read API (instant, non-blocking) ----------
    def snapshot(self, index_key: str) -> Optional[Dict[str, Any]]:
        snap = self._snapshots.get(index_key.upper())
        return snap.to_dict() if snap else None

    def expiry_list(self, index_key: str) -> List[str]:
        return self._expiries.get(index_key.upper(), [])

    def set_expiry(self, index_key: str, expiry: str) -> None:
        self._expiry_override[index_key.upper()] = expiry

    # ---------- fetching ----------
    async def _fetch_one(self, index_key: str, force: bool = False) -> None:
        key = index_key.upper()
        inst = get_index(key)
        if inst is None:
            return
        # Cooldown: skip if we fetched this underlying too recently (unless the
        # background poller forces it on its own schedule).
        now = time.monotonic()
        if not force and (now - self._last_fetch.get(key, 0.0)) < self._min_gap:
            return
        self._last_fetch[key] = now
        client = get_dhan_client()
        # Expiry lists change at most once a day, so fetch ONCE and cache it.
        # Re-fetching every cycle was doubling our Dhan calls and causing 429s.
        if key not in self._expiries:
            try:
                self._expiries[key] = await client.get_expiry_list(inst.sec_id)
            except DhanAPIError as exc:
                self._record_error(key, f"expiry list: {exc.error_message}")
                return

        expiry = self._expiry_override.get(key) or (
            self._expiries[key][0] if self._expiries[key] else None
        )
        if not expiry:
            self._record_error(key, "no active expiry")
            return

        try:
            raw = await client.get_option_chain(inst.sec_id, expiry)
        except DhanAPIError as exc:
            self._record_error(key, exc.error_message)
            return

        snap = _transform_chain(key, expiry, raw)
        async with self._lock:
            self._snapshots[key] = snap

    def _record_error(self, key: str, message: str) -> None:
        prev = self._snapshots.get(key)
        if prev is not None:
            prev.error = message
        else:
            self._snapshots[key] = ChainSnapshot(key, "", 0.0, [], error=message)
        logger.warning("Option chain error for %s: %s", key, message)

    # ---------- background loop ----------
    async def _loop(self) -> None:
        s = get_settings()
        keys = [k.upper() for k in s.oc_polled_indices if k.upper() in INDEX_REGISTRY]
        if not keys:
            keys = list(INDEX_REGISTRY.keys())[:1]
        # `interval` is the full gap between refreshes of the SAME underlying.
        # Dhan's free tier allows 1 optionchain req / 3s per underlying, so we
        # apply a hard floor of 3.5s (with a little headroom) per underlying.
        interval = max(3.5, float(s.oc_poll_interval_seconds))
        per_key_gap = interval / max(len(keys), 1)

        while self._running:
            for key in keys:
                if not self._running:
                    break
                await self._fetch_one(key, force=True)
                # Space out keys so two underlyings never burst together.
                await asyncio.sleep(per_key_gap)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._warm_then_loop())

    async def _warm_then_loop(self) -> None:
        """Warm each index one at a time (staggered) to avoid a startup burst."""
        keys = [k.upper() for k in get_settings().oc_polled_indices]
        for key in keys:
            if not self._running:
                return
            await self._fetch_one(key, force=True)
            # Space warmups so Dhan never sees back-to-back calls.
            await asyncio.sleep(self._min_gap)
        await self._loop()

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None


engine = OptionChainEngine()
