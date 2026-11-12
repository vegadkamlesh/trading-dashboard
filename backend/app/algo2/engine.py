"""Algo 2 - quantitative breakout engine (CALL and PUT, mirrored rules).

Reuses the Algo 1 execution core (``AlgoEngine``): order placement, broker
reconcile, protective SL, Telegram notifications, journal, crash recovery and
Master/Child dispatch. Only the *signal* and the *trade management* differ:

Entry (CALL; PUT is the mirror image), every ENABLED condition must pass:
  news freeze off, inside a trade window, PCR outside the chop band,
  net delta rising, net GEX negative, PE OI velocity > spike, CE OI unwinding,
  HSMI > 0, RELIANCE+HDFCBANK+ICICIBANK in sync, no heavyweight divergence,
  futures above VWAP+1.5sd and above the value-area high, basis not in
  discount, India VIX not spiking.
Trigger: spot breaks the last closed 5-minute candle high + buffer.
Contract: ITM strike with |delta| closest to 0.70 (live chain greeks).
Stop: breakout candle low, capped so the loss <= max_stop_loss_pct of balance.
T1: highest-OI call wall (1:2 R:R when the wall is missing/too close):
    book t1_pct of the lots, stop -> cost, rest trails VWAP towards 2R.
    One lot: full exit at T1.
Exits: data SL (CE OI velocity overtakes PE), 20-minute stagnation exit,
time exit, rupee exits, Dhan-parked protective SL.

Algo 2 always trades the INTRADAY product; Algo 1 trades MARGIN. Broker
reconcile filters by product, so the two algos never adopt each other's qty.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException

from .. import algo as base
from ..accounts import ACCOUNT_IDS, AccountId, account_credentials, account_scope
from ..algo import (
    MAX_PRICE_AGE, TERMINAL_ORDERS, AlgoConfig, AlgoEngine, DayLeg, Position,
    _as_float, _as_int, now_ist, session_for,
)
from ..algo_store import AlgoStore, base_dir
from ..config import get_settings
from ..dhan_client import DhanAPIError
from ..feed import feed
from ..instruments import get_index, lot_size_for
from . import calculations as calc
from .daily_cache import Algo2DailyCache
from .market_data import HEAVYWEIGHT_IDS, INDIA_VIX_ID, VELOCITY_WINDOW, Algo2MarketData
from .news_events import NewsEventFetcher

logger = logging.getLogger("dhan.algo2")

DATA_DIR = os.path.join(os.path.dirname(base_dir()), "algo2")
SIGNAL_LOOP_SECONDS = 1.0

CONDITIONS: List[Tuple[str, str]] = [
    ("news", "No news/event freeze (+/-15 min)"),
    ("time_window", "Inside a trade window"),
    ("pcr_filter", "PCR outside chop band"),
    ("net_delta", "Net delta trending with the trade (5 min)"),
    ("gex_negative", "Net GEX negative (expansion zone)"),
    ("oi_velocity", "Opposite-side OI velocity > spike (5 min)"),
    ("oi_unwinding", "Same-side OI unwinding (5 min)"),
    ("hsmi", "Heavyweight momentum (HSMI) with the trade"),
    ("trio_sync", "RELIANCE + HDFCBANK + ICICIBANK in sync"),
    ("heavyweight_divergence", "No heavyweight divergence"),
    ("vwap_band", "Futures beyond VWAP +/-1.5 sd band"),
    ("market_profile", "Futures beyond value area (VAH/VAL)"),
    ("basis", "Futures basis confirms (no discount divergence)"),
    ("vix", "India VIX stable (< 3% intraday move against)"),
]
GATE_CONDITIONS = frozenset({"news", "time_window", "heavyweight_divergence", "vix"})   # always required
CONFIG_VERSION = 4   # v2: gate + score entry, OI velocity 10%, VWAP band 1.0 sd
#                      v3: score >= 7, 09:45-15:00, 2R full exit, optional rules off
#                      v4: software-only stop; Dhan mishandled Algo 2 protective SL-M orders
TRIO = ("RELIANCE", "HDFCBANK", "ICICIBANK")
SL_REASONS = {"STOP", "DATA_SL", "PROTECTIVE_SL", "PROTECTIVE_LEVEL", "LOSS_RS"}
GROUP_PHASES = ("scanning", "waiting_window", "news_freeze", "trades_done", "sl_stop", "market_closed")
PRIORITY_EXIT_SECONDS = 6.0     # max wait for Algo 1's SELL fill before giving up the Algo 2 entry
PRIORITY_POLL_SECONDS = 0.1
PRIORITY_EVENT_WAIT = 0.25      # order-stream alert normally wakes the fill check well before this
PRIORITY_ENTRY_TRIES = 5        # broker funds can lag the SELL fill by a few hundred ms
_BACKGROUND: set = set()


def _default_conditions() -> Dict[str, bool]:
    return {key: True for key, _ in CONDITIONS}


@dataclass
class Algo2Config(AlgoConfig):
    target_only: bool = False
    product_type: str = "INTRADAY"
    max_affordable: bool = True
    entry_cutoff: str = "15:00"
    exit_time: str = "15:15"
    max_trades: int = 2
    trade_limit: bool = False         # False = no daily trade cap (max_trades ignored)
    protective_sl: bool = False
    protective_sl_mult: float = 2.0
    conditions: Dict[str, bool] = field(default_factory=_default_conditions)
    trade_windows: str = "09:45-15:00"
    entry_buffer: Dict[str, float] = field(default_factory=lambda: {"NIFTY": 0.5, "SENSEX": 1.5})
    max_stop_loss_pct: float = 0.75
    capital_sl_cap: bool = False      # operator choice: SL = breakout candle low/high only
    stop_after_sl: bool = False       # True: no more entries today once a trade exits on a stop
    priority_over_algo1: bool = True  # LIVE: exit Algo 1's position when its margin blocks an Algo 2 entry
    status_report_minutes: int = 5
    max_hold_minutes: int = 0         # 0 = off; else exit if T1 not reached within this many minutes
    oi_velocity_spike: float = 10.0
    min_score: int = 7                # entry needs every gate + at least this many score conditions
    config_version: int = 3
    data_sl: bool = False
    data_sl_gap_pct: float = 10.0
    oi_wall_target: bool = False      # False: target = 2R; True: highest-OI wall if >= 2R away
    event_window_minutes: int = 15
    target_delta: float = 0.70
    partial_booking: bool = False
    t1_pct: float = 70.0
    pcr_chop_low: float = 0.85
    pcr_chop_high: float = 1.10
    vix_spike_pct: float = 3.0
    oi_strikes: int = 5
    vwap_band_sd: float = 1.0
    profile_bin: Dict[str, float] = field(default_factory=lambda: {"NIFTY": 5.0, "SENSEX": 10.0})
    nifty_weights: Dict[str, float] = field(default_factory=lambda: {
        "RELIANCE": 0.101, "HDFCBANK": 0.062, "BHARTIARTL": 0.060,
        "SBIN": 0.050, "ICICIBANK": 0.048, "TCS": 0.047})
    sensex_weights: Dict[str, float] = field(default_factory=lambda: {
        "RELIANCE": 0.111, "BHARTIARTL": 0.081, "HDFCBANK": 0.075,
        "ICICIBANK": 0.070, "SBIN": 0.058})


@dataclass
class Algo2Position(Position):
    t1_spot: float = 0.0
    t1_done: bool = False
    t1_pending: bool = False
    t1_qty: int = 0
    t1_premium: Optional[float] = None
    t1_order_id: Optional[str] = None
    t1_correlation_id: Optional[str] = None
    t1_time: str = ""
    next_t1_try_at: float = 0.0


def parse_windows(text: str) -> List[Tuple[int, int]]:
    out: List[Tuple[int, int]] = []
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            a, b = part.split("-")
            start = datetime.strptime(a.strip(), "%H:%M")
            end = datetime.strptime(b.strip(), "%H:%M")
        except ValueError as exc:
            raise ValueError(f"Bad trade window '{part}' (use HH:MM-HH:MM)") from exc
        s, e = start.hour * 60 + start.minute, end.hour * 60 + end.minute
        if e <= s:
            raise ValueError(f"Trade window '{part}' ends before it starts")
        out.append((s, e))
    return out


class Algo2Store(AlgoStore):
    """Algo 2 journals are research data: kept forever (Algo 1 keeps 7 days)."""

    def prune_journals(self, keep_days: int = 0) -> int:
        return 0


class Algo2Engine(AlgoEngine):
    ALGO_LABEL = "Algo 2"
    ORDER_TAG = "AL2"
    POSITION_CLS = Algo2Position

    def __init__(self, group: "Algo2Group", account_id: AccountId) -> None:
        self._group_ref = group
        self._store_ref = Algo2Store(group.dir if account_id == "master" else os.path.join(group.dir, account_id))
        super().__init__(account_id)
        self.cfg = Algo2Config()

    # ------------------------------------------------------------ hooks
    def _is_dry(self) -> bool:
        return self._group_ref.dry_run

    @property
    def _store(self):
        return self._store_ref

    @property
    def _group(self):
        return self._group_ref

    async def _maybe_compute_or(self, force: bool = False) -> None:
        return None

    def _maybe_open_next_trade(self, today: str, minute: int) -> None:
        return None

    async def _look_for_entry(self) -> None:
        return None   # entries come only from Algo2Group's signal loop

    async def _tick_supervisor_locked(self) -> None:
        await super()._tick_supervisor_locked()
        if self._position is None and self.enabled and self.phase in {
                "watching", "building_or", "day_done", *GROUP_PHASES}:
            self.phase = self._group_ref.flat_phase()

    def _phase_label(self) -> str:
        return {
            "scanning": "Scanning - every enabled condition is checked each second",
            "waiting_window": "Outside the trade windows - waiting",
            "news_freeze": "News/event freeze active - no entries",
            "trades_done": "Max trades for today done",
            "sl_stop": "Stop-loss hit today - no more entries today",
            "market_closed": "Market closed",
            "in_trade": "In a trade - managing stop, target and enabled exit rules",
        }.get(self.phase) or super()._phase_label()

    def _next_actions(self) -> List[str]:
        p = self._position
        if p is None or p.execution_state in {"entry_pending", "exit_pending"}:
            if p is None and self.enabled:
                return self._group_ref.snapshot_lines()
            return super()._next_actions()
        sign = "above" if p.option_type == "CALL" else "below"
        out = [f"Software spot stop {p.stop_spot:,.1f} | "
               f"{'T2' if p.t1_done else 'T1'} {p.target_spot:,.1f} ({sign} entry)."]
        if not p.t1_done:
            if self.cfg.max_hold_minutes > 0:
                left = self.cfg.max_hold_minutes * 60 - (time.time() - (p.entry_confirmed_ts or p.entry_ts))
                out.append(f"{self.cfg.max_hold_minutes}-minute rule: exit if T1 not hit in "
                           f"{max(0, left) / 60:.1f} min.")
        else:
            out.append(f"T1 booked {p.t1_qty} @ {p.t1_premium}; stop at cost/VWAP trail.")
        out.append(f"Forced square-off at {self._sess_val('exit_time')}.")
        return out

    async def arm(self) -> Dict[str, Any]:
        algo1 = base.engines.items.get(self.account_id)
        if algo1 is not None and algo1.cfg.product_type == "INTRADAY":
            msg = ("ARM REFUSED: Algo 1 is also set to INTRADAY on this account. Algo 2 trades INTRADAY and "
                   "Algo 1 must stay on MARGIN so broker positions of the two algos never mix.")
            self._log(msg, level="error")
            return {**self.status(), "armed": False, "armError": {"reason": msg}}
        return await super().arm()

    # ----------------------------------------------------------- config
    def _restore(self) -> None:
        saved = (self._store.load_state() or {}).get("config") or {}
        super()._restore()
        version = int(saved.get("config_version") or 1) if saved else CONFIG_VERSION
        if version < 2:
            # One-time move to the gate + score rules; later operator edits are kept.
            self.cfg.oi_velocity_spike, self.cfg.vwap_band_sd = 10.0, 1.0
            self.cfg.conditions = {**_default_conditions(), **(self.cfg.conditions or {}),
                                   **{k: True for k in GATE_CONDITIONS}}
        if version < 3:
            # One-time move to the 2R rules backtested on 2026-11-11.
            self.cfg.min_score, self.cfg.trade_windows = 7, "09:45-15:00"
            self.cfg.entry_cutoff, self.cfg.exit_time = "15:00", "15:15"
            self.cfg.data_sl = self.cfg.stop_after_sl = self.cfg.trade_limit = False
            self.cfg.oi_wall_target = self.cfg.partial_booking = False
            self.cfg.max_hold_minutes = 0
            self.cfg.config_version = CONFIG_VERSION
            self._persist()
        if version < 4 or self.cfg.protective_sl:
            self.cfg.protective_sl = False
            self.cfg.config_version = CONFIG_VERSION
            self._persist()

    def set_config(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        patch = dict(patch or {})
        patch["product_type"] = "INTRADAY"
        patch["target_only"] = False
        # Until Dhan's SL-M handling is verified, Algo 2 exits only via its software spot stop.
        patch["protective_sl"] = False
        patch["config_version"] = CONFIG_VERSION
        if isinstance(patch.get("conditions"), dict):
            patch["conditions"] = {**patch["conditions"], **{k: True for k in GATE_CONDITIONS}}
        merged = {**asdict(self.cfg), **{k: v for k, v in patch.items() if v is not None}}
        validate_algo2_config(merged)
        return super().set_config(patch)

    # --------------------------------------------------------- contract
    async def _prepare_contract(self, leg: DayLeg, direction: Optional[str] = None) -> None:
        """ITM contract whose |delta| is closest to target_delta (CALL below spot, PUT above)."""
        inst = get_index(leg.index_key)
        snap = base.chain_engine.snapshot(leg.index_key)
        if not snap or not snap.get("rows"):
            try:
                await base.chain_engine.warm(leg.index_key)
            except Exception:  # noqa: BLE001
                pass
            snap = base.chain_engine.snapshot(leg.index_key)
        if not snap or not snap.get("rows") or inst is None:
            leg.note = "option chain not ready"
            return
        spot = leg.spot or float(snap.get("underlyingLtp") or 0)
        if not spot:
            leg.note = "no spot"
            return
        leg.expiry = snap.get("expiry")
        sides = (direction,) if direction in ("CALL", "PUT") else ("CALL", "PUT")
        for side in sides:
            row = calc.pick_delta_strike(snap["rows"], spot, side, self.cfg.target_delta)
            if row is None:
                leg.note = f"no ITM {side} strike with delta near {self.cfg.target_delta}"
                continue
            node = row["ce" if side == "CALL" else "pe"] or {}
            sec = _as_int(node.get("securityId"))
            if not sec:
                continue
            feed.subscribe(sec, inst.derivative_segment)
            if direction is None:
                continue
            leg.strike = float(row["strike"])
            leg.security_id = sec
            leg.option_ltp = _as_float(node.get("ltp"))
            leg.prepared_side = side
            delta = abs(_as_float((node.get("greeks") or {}).get("delta")) or 0)
            self._log(f"{leg.index_key} contract ready: {int(leg.strike)} {'CE' if side == 'CALL' else 'PE'} "
                      f"(ITM, delta {delta:.2f}) premium ~{leg.option_ltp}",
                      level="info", index=leg.index_key, side=side, strike=leg.strike)
            return

    # ----------------------------------------------------------- levels
    def _levels(self, leg: DayLeg, direction: str, spot: float, rng: float,
                sizing: Dict[str, Any]) -> tuple:
        sign = 1 if direction == "CALL" else -1
        ctx = self._group_ref.signal_ctx.get(leg.index_key) or {}
        candle_stop = ctx.get("candleLow") if direction == "CALL" else ctx.get("candleHigh")
        if not candle_stop or sign * (spot - candle_stop) <= 0:
            candle_stop = spot - sign * rng
        dist = abs(spot - candle_stop)
        delta = self._delta_for(leg, direction) or self.cfg.target_delta
        qty, balance = int(sizing.get("quantity") or 0), float(sizing.get("balance") or 0)
        if self.cfg.capital_sl_cap and qty > 0 and balance > 0 and delta > 0:
            cap = balance * self.cfg.max_stop_loss_pct / 100.0 / (delta * qty)
            if cap < dist:
                self._log(f"{leg.index_key}: candle stop {dist:,.1f} pts capped to {cap:,.1f} pts "
                          f"({self.cfg.max_stop_loss_pct}% of Rs{balance:,.0f} at delta {delta:.2f} x{qty})",
                          index=leg.index_key)
                dist = cap
        dist = max(dist, 0.05)
        wall = ctx.get("wall") if self.cfg.oi_wall_target else None
        t1 = wall if wall and sign * (wall - spot) >= 2 * dist else spot + sign * 2 * dist
        return round(t1, 2), round(spot - sign * dist, 2)

    async def _enter(self, leg: DayLeg, direction: str) -> None:
        await super()._enter(leg, direction)
        pos = self._position
        if isinstance(pos, Algo2Position) and not pos.t1_spot:
            pos.t1_spot = pos.target_spot
            self._persist()

    # ------------------------------------------------- priority over Algo 1
    def _algo1_blocking_position(self) -> Optional[Position]:
        """Algo 1's live position on this account that Algo 2 may close to free margin."""
        if not self.cfg.priority_over_algo1 or self._is_dry():
            return None
        algo1 = base.engines.items.get(self.account_id)
        pos = getattr(algo1, "_position", None)
        # "attention" = still held at Dhan but e.g. its broker SL was rejected; the safe exit re-checks qty.
        if pos is None or pos.dry_run or pos.execution_state not in {"open", "exit_pending", "attention"}:
            return None
        return pos

    async def _convert_algo1_stop_to_market(self, algo1: Any, pos: Position) -> bool:
        """One broker call: turn Algo 1's parked SL-M SELL into a MARKET SELL.

        Same order, so there is never a second SELL (no cancel/confirm/re-read/new-order
        round trips). Any failure restores the protective state and the caller falls back
        to the normal safe exit, which re-verifies that order first."""
        oid = pos.protective_order_id
        if (not oid or str(oid).startswith("DRY-") or pos.exit_order_id or pos.exit_correlation_id
                or pos.protective_cancel_requested or pos.partial_exit or pos.execution_state != "open"):
            return False
        saved = (pos.protective_order_id, pos.protective_correlation_id, pos.exit_reason)
        pos.exit_reason, pos.execution_state, pos.exit_order_id = "ALGO2_PRIORITY", "exit_pending", str(oid)
        pos.protective_order_id = pos.protective_correlation_id = None
        algo1._persist()
        try:
            await base.get_dhan_client().modify_order(str(oid), {
                "orderType": "MARKET", "legName": "", "quantity": int(pos.quantity), "price": 0,
                "disclosedQuantity": 0, "triggerPrice": 0, "validity": "DAY"})
            return True
        except DhanAPIError as exc:
            pos.protective_order_id, pos.protective_correlation_id, pos.exit_reason = saved
            pos.execution_state, pos.exit_order_id = "open", None
            algo1._persist()
            self._log(f"PRIORITY: fast SL->MARKET modify failed ({exc.error_message}); using safe exit",
                      level="warn", index=pos.index_key)
            return False

    async def _await_order_change(self, wait: float) -> None:
        waiter = getattr(base.get_dhan_client(), "wait_order_event", None)
        if asyncio.iscoroutinefunction(waiter):
            await waiter(wait)
        else:
            await asyncio.sleep(PRIORITY_POLL_SECONDS)

    async def free_margin_from_algo1(self, key: str) -> bool:
        """Exit Algo 1 now; True once its SELL is filled (funds free) or it is already flat."""
        pos = self._algo1_blocking_position()
        if pos is None:
            return False
        algo1 = base.engines.items[self.account_id]
        client = base.get_dhan_client()
        clear = getattr(client, "clear_order_event", None)
        t0 = time.perf_counter()
        self._log(f"PRIORITY: {key} signal needs the margin held by Algo 1 ({pos.index_key} {pos.option_type} "
                  f"{int(pos.strike)} x{pos.quantity}) -> exiting Algo 1 now", level="warn", index=key)
        algo1._log("Exit requested by Algo 2 priority (Algo 2 signal needs the margin)", level="warn")
        filled, fast, sent_ms = False, False, None
        async with algo1._execution_lock:
            deadline = time.monotonic() + PRIORITY_EXIT_SECONDS
            while time.monotonic() < deadline and algo1._position is pos:
                if callable(clear):
                    clear()
                if not (pos.exit_order_id or pos.exit_correlation_id):
                    fast = await self._convert_algo1_stop_to_market(algo1, pos)
                    if not fast:
                        await algo1._exit_position_locked(pos, "ALGO2_PRIORITY")
                    sent_ms = (time.perf_counter() - t0) * 1000
                    if algo1._position is not pos:
                        break
                    if not (pos.exit_order_id or pos.exit_correlation_id):
                        await asyncio.sleep(PRIORITY_POLL_SECONDS)
                        continue
                try:
                    order = await algo1._order_state(pos.exit_order_id, pos.exit_correlation_id)
                except DhanAPIError:
                    order = {}
                status = str(order.get("orderStatus") or "").upper()
                if status == "TRADED":
                    filled = True
                    break
                if status in TERMINAL_ORDERS:
                    if fast and pos.exit_order_id:
                        # Modified stop died without a fill: give Algo 1's normal exit a clean retry.
                        pos.exit_order_id, pos.execution_state, fast = None, "open", False
                        continue
                    break
                await self._await_order_change(PRIORITY_EVENT_WAIT)
            filled = filled or algo1._position is not pos
        # Algo 1's books are settled after Algo 2 is in, off the critical path.
        task = asyncio.create_task(self._reconcile_algo1_later(algo1), name=f"{self.account_id}-algo1-after-priority")
        _BACKGROUND.add(task)
        task.add_done_callback(_BACKGROUND.discard)
        ms = (time.perf_counter() - t0) * 1000
        if not filled:
            self._log(f"PRIORITY: Algo 1 exit not filled within {PRIORITY_EXIT_SECONDS:g}s - Algo 2 {key} entry "
                      f"skipped. Check Dhan.", level="error", index=key)
            return False
        how = "SL order modified to MARKET" if fast else "safe exit (cancel SL + MARKET SELL)"
        self._log(f"PRIORITY: Algo 1 exit filled in {ms:.0f} ms ({how}; sent at {sent_ms or 0:.0f} ms) "
                  f"-> entering Algo 2 now", level="success", index=key)
        return True

    async def _reconcile_algo1_later(self, algo1: Any) -> None:
        try:
            with account_scope(self.account_id):
                async with algo1._execution_lock:
                    await algo1._reconcile_locked()
        except Exception:  # noqa: BLE001
            logger.exception("account=%s Algo 1 reconcile after priority exit failed", self.account_id)

    # ------------------------------------------------------- management
    def _t1_lots(self, pos: Algo2Position) -> int:
        if not self.cfg.partial_booking or pos.lots < 2:
            return 0
        return min(pos.lots - 1, max(1, int(math.floor(pos.lots * self.cfg.t1_pct / 100.0 + 1e-9))))

    async def _manage_position(self) -> None:
        pos = self._position
        if await self._maybe_rupee_exit_locked():
            return
        if not isinstance(pos, Algo2Position) or pos.spot is None or pos.execution_state not in {"open", "attention"}:
            return
        if pos.t1_pending:
            await self._resolve_t1(pos)
            if self._position is not pos or pos.t1_pending:
                return
        inst = get_index(pos.index_key)
        age = feed.age(inst.sec_id, "IDX_I") if inst else None
        if age is None or age > MAX_PRICE_AGE:
            return
        sign = 1 if pos.option_type == "CALL" else -1
        moved = sign * (pos.spot - pos.entry_spot)
        pos.mfe, pos.mae = max(pos.mfe, moved), min(pos.mae, moved)

        if self.cfg.data_sl:
            vel = self._group_ref.data.oi_velocity(pos.index_key)
            same, other = (vel.get("ce"), vel.get("pe")) if pos.option_type == "CALL" else (vel.get("pe"), vel.get("ce"))
            if same is not None and other is not None and same > 0 and same - other >= self.cfg.data_sl_gap_pct:
                self._log(f"{pos.index_key}: DATA SL - {'CE' if sign > 0 else 'PE'} OI velocity {same:+.1f}% "
                          f"overtook {'PE' if sign > 0 else 'CE'} {other:+.1f}% (trap) -> exit",
                          level="warn", index=pos.index_key)
                await self._exit_position_locked(pos, "DATA_SL")
                return

        held_for = time.time() - (pos.entry_confirmed_ts or pos.entry_ts)
        if not pos.t1_done and self.cfg.max_hold_minutes > 0 and held_for >= self.cfg.max_hold_minutes * 60:
            self._log(f"{pos.index_key}: {self.cfg.max_hold_minutes}-minute rule - T1 not reached "
                      f"(moved {moved:+,.1f} pts) -> exit to avoid theta decay", level="warn", index=pos.index_key)
            await self._exit_position_locked(pos, "STAGNANT_20M")
            return

        if pos.t1_done:
            vwap_spot = self._group_ref.vwap_spot(pos.index_key)
            if vwap_spot is not None and sign * (vwap_spot - pos.stop_spot) > 0 and sign * (pos.spot - vwap_spot) > 0:
                pos.stop_spot = round(vwap_spot, 2)

        if sign * (pos.spot - pos.stop_spot) <= 0:
            reason = "COST_STOP" if pos.t1_done and abs(pos.stop_spot - pos.entry_spot) < 1e-6 else (
                "TRAIL_STOP" if pos.t1_done else "STOP")
            await self._exit_position_locked(pos, reason)
            return
        if sign * (pos.spot - pos.target_spot) >= 0:
            if pos.t1_done or self._t1_lots(pos) == 0:
                await self._exit_position_locked(pos, "TARGET" if pos.t1_done else "T1_FULL")
            elif time.time() >= pos.next_t1_try_at:
                await self._book_t1(pos)

    async def _book_t1(self, pos: Algo2Position) -> None:
        lots = self._t1_lots(pos)
        lot = max(1, pos.quantity // max(pos.lots, 1))
        qty = lots * lot
        if qty <= 0 or qty >= pos.quantity:
            await self._exit_position_locked(pos, "T1_FULL")
            return
        if not await self._cancel_protective(pos):
            return
        if self._position is not pos or pos.exit_order_id or pos.execution_state == "exit_pending":
            return
        pos.t1_qty = qty
        pos.t1_pending = True
        pos.t1_correlation_id = f"{self.ORDER_TAG}-T-{uuid.uuid4().hex[:18]}"
        self._persist()
        self._log(f"{pos.index_key}: T1 {pos.target_spot:,.1f} hit -> booking {lots} lot(s) x{qty} "
                  f"({self.cfg.t1_pct:g}%)", level="success", index=pos.index_key)
        res = await self._send_order(
            side="SELL", security_id=pos.security_id, qty=qty,
            segment=self._segment_for(pos.index_key), order_type="MARKET",
            correlation_id=pos.t1_correlation_id, tag="t1", dry_run=pos.dry_run,
        )
        if not res.get("ok"):
            if res.get("uncertain"):
                pos.execution_error = "T1 SELL outcome unknown; resolving by correlation ID (no repeat SELL)."
                self._persist()
                return
            pos.t1_pending = False
            pos.t1_correlation_id = None
            pos.next_t1_try_at = time.time() + 5.0
            pos.execution_error = f"T1 SELL rejected: {res.get('error')}"
            self._log(pos.execution_error, level="error", index=pos.index_key)
            if self.cfg.protective_sl:
                await self._place_protective_sl(pos)
            self._persist()
            return
        pos.t1_order_id = res.get("orderId")
        if pos.dry_run:
            self._apply_t1_fill(pos, qty, pos.premium or pos.entry_premium)
        else:
            await self._resolve_t1(pos)

    async def _resolve_t1(self, pos: Algo2Position) -> None:
        if pos.dry_run:
            return
        try:
            order = await self._order_state(pos.t1_order_id, pos.t1_correlation_id)
        except DhanAPIError as exc:
            pos.execution_error = f"T1 order status unknown: {exc.error_message}"
            return
        pos.t1_order_id = str(order.get("orderId") or pos.t1_order_id or "") or None
        status = str(order.get("orderStatus") or "").upper()
        if status not in TERMINAL_ORDERS:
            return
        filled = int(order.get("filledQty") or (pos.t1_qty if status == "TRADED" else 0))
        if filled > 0:
            self._apply_t1_fill(pos, filled, _as_float(order.get("averageTradedPrice")) or pos.premium or pos.entry_premium)
            return
        pos.t1_pending = False
        pos.t1_correlation_id = pos.t1_order_id = None
        pos.next_t1_try_at = time.time() + 5.0
        pos.execution_error = f"T1 SELL {status}: {order.get('omsErrorDescription') or 'no fill'}"
        self._log(pos.execution_error, level="error", index=pos.index_key)
        if self.cfg.protective_sl:
            await self._place_protective_sl(pos)
        self._persist()

    def _apply_t1_fill(self, pos: Algo2Position, qty: int, price: float) -> None:
        lot = max(1, pos.quantity // max(pos.lots, 1))
        pos.entry_quantity = pos.entry_quantity or pos.quantity
        pos.quantity = max(0, pos.quantity - qty)
        pos.lots = max(0, pos.quantity // lot)
        pos.t1_qty, pos.t1_premium = qty, round(price, 2)
        pos.t1_done, pos.t1_pending = True, False
        pos.t1_time = now_ist().strftime("%H:%M:%S")
        pos.execution_error = ""
        sign = 1 if pos.option_type == "CALL" else -1
        risk = pos.risk_points
        pos.stop_spot = pos.entry_spot
        pos.target_spot = round(pos.entry_spot + sign * max(2 * risk, sign * (pos.t1_spot - pos.entry_spot) + risk), 2)
        self._log(f"T1 BOOKED {pos.index_key} {pos.option_type} {int(pos.strike)} x{qty} @ {price:.2f} "
                  f"(Rs{(price - pos.entry_premium) * qty:,.0f}); {pos.quantity} left, stop -> cost "
                  f"{pos.stop_spot:,.1f}, T2 {pos.target_spot:,.1f}",
                  level="success", notification="algo_exit", index=pos.index_key)
        self._persist()
        if pos.quantity > 0 and self.cfg.protective_sl:
            asyncio.ensure_future(self._place_protective_after_t1(pos))

    async def _place_protective_after_t1(self, pos: Algo2Position) -> None:
        async with self._execution_lock:
            if self._position is pos and not pos.protective_order_id and not pos.protective_correlation_id:
                await self._place_protective_sl(pos)

    async def _reconcile_locked(self) -> Dict[str, Any]:
        pos = self._position
        if isinstance(pos, Algo2Position) and pos.t1_pending and not pos.dry_run:
            await self._resolve_t1(pos)
            if pos.t1_pending:
                return {"checkedAt": now_ist().strftime("%H:%M:%S"), "t1Pending": True}
        return await super()._reconcile_locked()

    async def _exit_position_locked(self, pos: Position, reason: str) -> None:
        if isinstance(pos, Algo2Position) and pos.t1_pending and not pos.dry_run:
            await self._resolve_t1(pos)
            if pos.t1_pending:
                return
        await super()._exit_position_locked(pos, reason)

    def _trade_pnl(self, pos: Position, exit_premium: float) -> float:
        if isinstance(pos, Algo2Position) and pos.t1_done and pos.t1_premium is not None:
            return ((pos.t1_premium - pos.entry_premium) * pos.t1_qty
                    + (exit_premium - pos.entry_premium) * pos.quantity)
        return super()._trade_pnl(pos, exit_premium)

    def _position_dict(self) -> Optional[Dict[str, Any]]:
        out = super()._position_dict()
        p = self._position
        if out is None or not isinstance(p, Algo2Position):
            return out
        held = time.time() - (p.entry_confirmed_ts or p.entry_ts)
        out.update({
            "t1Spot": p.t1_spot, "t1Done": p.t1_done, "t1Pending": p.t1_pending, "t1Qty": p.t1_qty,
            "t1Premium": p.t1_premium, "t1Time": p.t1_time,
            "heldSeconds": round(held), "holdLimitSeconds": self.cfg.max_hold_minutes * 60,
            "premiumPnl": round(self._trade_pnl(p, p.premium or p.entry_premium), 0),
        })
        return out


def validate_algo2_config(cfg: Dict[str, Any]) -> None:
    def positive(name: str, upper: Optional[float] = None) -> None:
        value = cfg.get(name)
        if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0 or (upper and value > upper):
            raise HTTPException(400, f"{name} must be a positive number{f' <= {upper}' if upper else ''}")
    for name, upper in (("max_stop_loss_pct", 10), ("oi_velocity_spike", 1000), ("data_sl_gap_pct", 1000),
                        ("target_delta", 0.99), ("t1_pct", 99), ("pcr_chop_low", 10), ("pcr_chop_high", 10),
                        ("vix_spike_pct", 100), ("vwap_band_sd", 10)):
        positive(name, upper)
    if cfg["pcr_chop_low"] >= cfg["pcr_chop_high"]:
        raise HTTPException(400, "pcr_chop_low must be below pcr_chop_high")
    for name in ("capital_sl_cap", "stop_after_sl", "data_sl", "partial_booking", "trade_limit", "oi_wall_target"):
        if not isinstance(cfg.get(name), bool):
            raise HTTPException(400, f"{name} must be true/false")
    for name, low, high in (("max_hold_minutes", 0, 375), ("event_window_minutes", 0, 240), ("oi_strikes", 1, 40),
                            ("status_report_minutes", 0, 60)):
        if not isinstance(cfg.get(name), int) or not low <= cfg[name] <= high:
            raise HTTPException(400, f"{name} must be an integer between {low} and {high}")
    score_total = len(CONDITIONS) - len(GATE_CONDITIONS)
    if not isinstance(cfg.get("min_score"), int) or isinstance(cfg.get("min_score"), bool) \
            or not 0 <= cfg["min_score"] <= score_total:
        raise HTTPException(400, f"min_score must be an integer between 0 and {score_total}")
    if not isinstance(cfg.get("conditions"), dict) or any(not isinstance(v, bool) for v in cfg["conditions"].values()):
        raise HTTPException(400, "conditions must map condition keys to true/false")
    for name in ("entry_buffer", "profile_bin", "nifty_weights", "sensex_weights"):
        values = cfg.get(name)
        if not isinstance(values, dict) or any(
                not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in values.values()):
            raise HTTPException(400, f"{name} must map names to non-negative numbers")
    for name in ("nifty_weights", "sensex_weights"):
        unknown = set(cfg[name]) - set(HEAVYWEIGHT_IDS)
        if unknown:
            raise HTTPException(400, f"{name}: unsupported stocks {sorted(unknown)} (supported {sorted(HEAVYWEIGHT_IDS)})")
    try:
        parse_windows(cfg.get("trade_windows") or "")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


# --------------------------------------------------------------------------- #
#  Permanent research log (never pruned): history/YYYY-MM-DD/*.jsonl
# --------------------------------------------------------------------------- #
class Algo2History:
    """Append-only daily cache of Algo 2 data for future rule tuning.

    reports.jsonl   - every 5-min situation report with per-index metrics
    metrics.jsonl   - one metrics row per index per minute
    chain.jsonl     - raw option chain (ATM +/-10: OI, LTP, IV, greeks) per index per minute
    breakouts.jsonl - every breakout (taken or skipped) with gates/score, plus a
                      follow-up "outcome" row 20 min later (move, MFE/MAE, 2R/stop hit first)
    candles/ + day_summary.json - end-of-day 1-min candles and summary (see daily_cache.py)
    """

    OUTCOME_MINUTES = 20
    CHAIN_STRIKES = 10

    def __init__(self, directory: str) -> None:
        self.dir = directory
        self._last_minute: Dict[str, str] = {}
        self._last_chain_minute: Dict[str, str] = {}
        self._seen: Dict[Tuple[str, float, str], bool] = {}
        self._pending: List[Dict[str, Any]] = []

    def chain_snapshot(self, now: datetime, key: str, spot: Optional[float], futures: Optional[float],
                       expiry: Optional[str], rows: List[Dict[str, Any]]) -> None:
        """Raw option chain (ATM +/-10) once per minute - Dhan has no historical chain/greeks."""
        minute = now.strftime("%Y-%m-%d %H:%M")
        if not rows or not spot or self._last_chain_minute.get(key) == minute:
            return
        self._last_chain_minute[key] = minute

        def leg(row: Dict[str, Any], side: str) -> Dict[str, Any]:
            lg = row.get(side) or {}
            g = lg.get("greeks") or {}
            return {"ltp": lg.get("ltp"), "oi": lg.get("oi"), "prevOi": lg.get("prevOi"),
                    "vol": lg.get("volume"), "iv": lg.get("iv"), "d": g.get("delta"), "g": g.get("gamma"),
                    "t": g.get("theta"), "v": g.get("vega")}
        strikes = [{"k": r.get("strike"), "ce": leg(r, "ce"), "pe": leg(r, "pe")}
                   for r in calc.near_rows(rows, spot, self.CHAIN_STRIKES)]
        self.append(now, "chain", {"time": now.isoformat(timespec="seconds"), "index": key, "spot": spot,
                                   "futures": futures, "expiry": expiry, "strikes": strikes})

    def append(self, now: datetime, name: str, row: Dict[str, Any]) -> None:
        try:
            folder = os.path.join(self.dir, now.date().isoformat())
            os.makedirs(folder, exist_ok=True)
            with open(os.path.join(folder, f"{name}.jsonl"), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, default=str, separators=(",", ":")) + "\n")
        except (OSError, TypeError, ValueError):
            logger.warning("Algo 2 history write failed (%s)", name, exc_info=True)

    @staticmethod
    def metrics_row(m: Dict[str, Any]) -> Dict[str, Any]:
        def checks(side: str) -> Dict[str, Any]:
            return {c["key"]: {"pass": c["pass"], "on": c["enabled"], "v": c.get("value")}
                    for c in (m.get("checks") or {}).get(side, [])}
        return {**{k: m.get(k) for k in ("spot", "candle", "triggerUp", "triggerDown", "breakout", "netDelta",
                                         "netDeltaPrev", "netGex", "pcr", "oiVelocity", "hsmi", "heavyweights",
                                         "vwap", "profile", "futures", "basis", "vixMovePct", "chainRows")},
                "score": m.get("score"), "checks": {s: checks(s) for s in ("CALL", "PUT")}}

    def breakout(self, now: datetime, key: str, side: str, m: Dict[str, Any], taken: bool,
                 blockers: List[str]) -> None:
        bar = float((m.get("candle") or {}).get("t") or 0)
        ident = (key, bar, side)
        if ident in self._seen and (not taken or self._seen[ident]):
            return
        self._seen[ident] = taken
        if len(self._seen) > 500:
            self._seen = dict(list(self._seen.items())[-200:])
        candle, spot = m.get("candle") or {}, m.get("spot")
        self.append(now, "breakouts", {"type": "breakout", "time": now.isoformat(timespec="seconds"),
                                       "index": key, "side": side, "taken": taken, "blockers": blockers,
                                       **self.metrics_row(m)})
        if not spot or not candle or any(p["id"] == ident for p in self._pending):
            return
        stop = float(candle["low"] if side == "CALL" else candle["high"])
        risk = abs(float(spot) - stop) or None
        sign = 1 if side == "CALL" else -1
        self._pending.append({"id": ident, "index": key, "side": side, "taken": taken, "start": now,
                              "entry": float(spot), "stop": stop,
                              "target": float(spot) + sign * 2 * risk if risk else None,
                              "best": 0.0, "worst": 0.0, "first": None})

    def on_tick(self, now: datetime, metrics: Dict[str, Dict[str, Any]]) -> None:
        minute = now.strftime("%Y-%m-%d %H:%M")
        for key, m in metrics.items():
            if self._last_minute.get(key) != minute:
                self._last_minute[key] = minute
                self.append(now, "metrics", {"time": now.isoformat(timespec="seconds"), "index": key,
                                             **self.metrics_row(m)})
        keep = []
        for p in self._pending:
            spot = (metrics.get(p["index"]) or {}).get("spot")
            move = None
            if spot:
                move = (float(spot) - p["entry"]) * (1 if p["side"] == "CALL" else -1)
                p["best"], p["worst"] = max(p["best"], move), min(p["worst"], move)
                if p["first"] is None:
                    if (p["side"] == "CALL" and spot <= p["stop"]) or (p["side"] == "PUT" and spot >= p["stop"]):
                        p["first"] = "stop"
                    elif p["target"] is not None and ((p["side"] == "CALL" and spot >= p["target"])
                                                      or (p["side"] == "PUT" and spot <= p["target"])):
                        p["first"] = "target_2R"
            if (now - p["start"]).total_seconds() < self.OUTCOME_MINUTES * 60:
                keep.append(p)
                continue
            self.append(p["start"], "breakouts", {
                "type": "outcome", "time": now.isoformat(timespec="seconds"), "index": p["index"],
                "side": p["side"], "taken": p["taken"], "breakoutTime": p["start"].isoformat(timespec="seconds"),
                "entrySpot": p["entry"], "stopSpot": p["stop"], "targetSpot": p["target"],
                "lastSpot": spot, "movePts": round(move, 2) if move is not None else None,
                "maxFavourablePts": round(p["best"], 2), "maxAdversePts": round(p["worst"], 2),
                "firstHit": p["first"] or "none"})
        self._pending = keep


# --------------------------------------------------------------------------- #
#  Group: settings, data, signal loop, dispatch to Master/Child
# --------------------------------------------------------------------------- #
class Algo2Group:
    def __init__(self, directory: str = DATA_DIR) -> None:
        self.dir = directory
        os.makedirs(directory, exist_ok=True)
        self.settings_path = os.path.join(directory, "group.json")
        self.dry_run = False
        self.saved_enabled = True     # default ON + LIVE until the operator turns it OFF
        defaults = tuple(a for a in ACCOUNT_IDS if a in get_settings().trading_default_accounts)
        self.entry_accounts: Tuple[AccountId, ...] = defaults or ("master",)
        self.signal_day: Optional[str] = None
        self.signal_count = 0
        self._load_settings()
        self.items: Dict[AccountId, Algo2Engine] = {a: Algo2Engine(self, a) for a in ACCOUNT_IDS}
        self.news = NewsEventFetcher(directory)
        self.data = Algo2MarketData(directory)
        self.signal_ctx: Dict[str, Dict[str, Any]] = {}
        self.metrics: Dict[str, Dict[str, Any]] = {}
        self.last_signal: Optional[Dict[str, Any]] = None
        self._attempted: Dict[str, Tuple[float, str]] = {}
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._dispatching = False
        self.loop_error: Optional[str] = None
        self.reports: List[Dict[str, str]] = []
        self._last_report_slot: Optional[str] = None
        self._blocked_logged: Dict[str, Tuple[float, str]] = {}
        self._blocked_recent: List[str] = []
        self.history = Algo2History(os.path.join(directory, "history"))
        self.cache = Algo2DailyCache(self.history.dir, self.data,
                                     lambda: {a: e._store for a, e in self.items.items()})
        # "What happens next" is refreshed only on the 5-min slot or on entry/exit, never every second.
        self.snapshot: Dict[str, Any] = {"time": None, "lines": []}
        self._snapshot_holding: Optional[bool] = None

    # ------------------------------------------------------- persistence
    def _load_settings(self) -> None:
        try:
            with open(self.settings_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        self.dry_run = bool(data.get("dryRun", False))
        self.saved_enabled = bool(data.get("enabled", True))
        accounts = tuple(a for a in data.get("accounts") or self.entry_accounts if a in ACCOUNT_IDS)
        self.entry_accounts = accounts or ("master",)
        self.signal_day = data.get("signalDay")
        self.signal_count = int(data.get("signalCount") or 0)

    def save_settings(self) -> None:
        tmp = self.settings_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"dryRun": self.dry_run, "enabled": self.enabled, "accounts": list(self.entry_accounts),
                       "signalDay": self.signal_day, "signalCount": self.signal_count}, fh, indent=2)
        os.replace(tmp, self.settings_path)

    @property
    def master(self) -> Algo2Engine:
        return self.items["master"]

    @property
    def cfg(self) -> Algo2Config:
        return self.master.cfg

    @property
    def enabled(self) -> bool:
        return any(self.items[a].enabled for a in self.entry_accounts)

    def has_position(self) -> bool:
        return any(e._position is not None for e in self.items.values())

    def signals_used(self, today: str) -> int:
        return self.signal_count if self.signal_day == today else 0

    # --------------------------------------------------------- lifecycle
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        for account, engine in self.items.items():
            with account_scope(account):
                await engine.start()
                engine.enabled = False
                engine._persist()
        if self.master.cfg.product_type != "INTRADAY":
            self.master.cfg.product_type = "INTRADAY"
        feed.add_listener(self.on_tick)
        if self.saved_enabled:
            await self._rearm_after_restart()
        self._task = asyncio.create_task(self._signal_loop(), name="algo2-signal")
        logger.info("Algo 2 started (paper=%s, accounts=%s)", self.dry_run, ",".join(self.entry_accounts))
        self._announce_startup()

    def _announce_startup(self) -> None:
        on = self.enabled
        armed = ",".join(a for a in self.entry_accounts if self.items[a].enabled) or "none"
        msg = (f"Algo 2 {'ON' if on else 'OFF'} - {'PAPER' if self.dry_run else 'LIVE'} after server start | "
               f"accounts {armed} | priority over Algo 1 {'ON' if self.cfg.priority_over_algo1 else 'OFF'}")
        if self.saved_enabled and not on:
            msg += " | ARM FAILED: Algo 2 will NOT trade, check the Algo 2 tab"
        event = "auto_armed" if on else ("auto_arm_failed" if self.saved_enabled else "algo_disarmed")
        with account_scope("master"):
            self.master._log(msg, level="success" if on else ("error" if self.saved_enabled else "warn"),
                             notification=event)

    async def _rearm_after_restart(self) -> None:
        for account in self.entry_accounts:
            if account != "master" and not all(account_credentials(account)):
                continue
            with account_scope(account):
                try:
                    await self.items[account].arm()
                    self.items[account]._log("Algo 2 re-armed after restart (it was ON before).",
                                             level="info")
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Algo 2 re-arm failed for %s", account)
                    self.items[account]._last_error = f"Re-arm after restart failed: {exc}"

    async def stop(self) -> None:
        self._running = False
        feed.remove_listener(self.on_tick)
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        await self.cache.cancel()
        for account, engine in self.items.items():
            with account_scope(account):
                await engine.stop()

    def on_tick(self, sid: int, price: float, segment: str) -> None:
        for account, engine in self.items.items():
            with account_scope(account):
                engine._on_tick(sid, price, segment)

    # --------------------------------------------------------- operator
    async def set_enabled(self, on: bool) -> Dict[str, Any]:
        if on:
            for account in self.entry_accounts:
                if account != "master" and not all(account_credentials(account)):
                    raise HTTPException(400, f"{account} account has no Dhan credentials")
            for account in self.entry_accounts:
                with account_scope(account):
                    result = await self.items[account].arm()
                if not result.get("enabled"):
                    for other, engine in self.items.items():
                        if engine.enabled:
                            with account_scope(other):
                                engine.disarm(f"{account} arm refused")
                    self.save_settings()
                    error = result.get("armError") or {}
                    raise HTTPException(409, f"{account}: arm refused - {error.get('reason') or 'see journal'}")
        else:
            for account, engine in self.items.items():
                if engine.enabled:
                    with account_scope(account):
                        engine.disarm("operator")
        self.save_settings()
        return self.status()

    def set_mode(self, dry_run: bool) -> Dict[str, Any]:
        if self.has_position():
            raise HTTPException(409, "Exit the open Algo 2 position before switching Paper/Live.")
        if self.enabled:
            raise HTTPException(409, "Turn Algo 2 OFF before switching Paper/Live.")
        self.dry_run = bool(dry_run)
        self.master._log(f"Algo 2 mode -> {'PAPER (dry-run)' if self.dry_run else 'LIVE'}",
                         level="warn", notification="mode_changed")
        self.save_settings()
        return self.status()

    def set_accounts(self, accounts: List[str]) -> Dict[str, Any]:
        chosen = tuple(a for a in ACCOUNT_IDS if a in set(accounts or []))
        if not chosen:
            raise HTTPException(400, "Select at least one account")
        if self.has_position() or self.enabled:
            raise HTTPException(409, "Turn Algo 2 OFF (and be flat) before changing accounts.")
        for account in chosen:
            if account != "master" and not all(account_credentials(account)):
                raise HTTPException(400, f"{account} account has no Dhan credentials")
        self.entry_accounts = chosen
        self.save_settings()
        return self.status()

    def set_config(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        if self.has_position():
            raise HTTPException(409, "Cannot change Algo 2 configuration while a position is open.")
        for account, engine in self.items.items():
            with account_scope(account):
                engine.set_config(patch)
        self.news.window_minutes = self.cfg.event_window_minutes
        return self.status()

    async def panic_exit(self) -> Dict[str, Any]:
        for account, engine in self.items.items():
            if engine._position is not None:
                with account_scope(account):
                    await engine.panic_exit("Algo 2 PANIC button")
        return self.status()

    # ------------------------------------------------------ signal loop
    def _windows(self) -> List[Tuple[int, int]]:
        try:
            return parse_windows(self.cfg.trade_windows)
        except ValueError:
            return []

    def in_window(self, minute: int) -> bool:
        return any(s <= minute < e for s, e in self._windows())

    def flat_phase(self) -> str:
        now = now_ist()
        minute = now.hour * 60 + now.minute
        sess = session_for(now)
        if not sess.trading_day or not sess.open_min <= minute < sess.close_min:
            return "market_closed"
        if self.sl_hit_today(now.date().isoformat()):
            return "sl_stop"
        if self.trades_done(now.date().isoformat()):
            return "trades_done"
        if self.news.is_news_freeze_active(now)[0]:
            return "news_freeze"
        if not self.in_window(minute) or minute >= self.master._cutoff_min(now):
            return "waiting_window"
        return "scanning"

    def trades_done(self, today: str) -> bool:
        return self.cfg.trade_limit and self.signals_used(today) >= self.cfg.max_trades

    def sl_hit_today(self, today: str) -> Optional[str]:
        """Text of today's first stop-loss exit when ``stop_after_sl`` is on, else None."""
        if not self.cfg.stop_after_sl:
            return None
        for engine in self.items.values():
            for t in engine._today_trades:
                if str(t.get("exitReason")) in SL_REASONS and str(t.get("date") or today) == today:
                    return (f"{t.get('index')} {t.get('direction')} exited {t.get('exitReason')} at "
                            f"{t.get('exitTime')}")
        return None

    def _next_window_text(self, minute: int) -> str:
        cutoff = self.master._cutoff_min(now_ist())
        for start, end in self._windows():
            if minute < start and start < cutoff:
                return f"next window opens {start // 60:02d}:{start % 60:02d}"
            if start <= minute < min(end, cutoff):
                return f"window open till {min(end, cutoff) // 60:02d}:{min(end, cutoff) % 60:02d}"
        return "no more trade windows today"

    def next_action_text(self) -> str:
        now = now_ist()
        phase = self.flat_phase()
        minute = now.hour * 60 + now.minute
        if phase == "market_closed":
            return "Market closed - scanning resumes in the first trade window of the next trading day."
        if phase == "sl_stop":
            return f"Stop-loss hit today ({self.sl_hit_today(now.date().isoformat())}) - no more trades today."
        if phase == "trades_done":
            return f"{self.cfg.max_trades} trade(s) done today - no more entries."
        if phase == "news_freeze":
            return f"News freeze: {self.news.is_news_freeze_active(now)[1]} - no entries until it clears."
        if phase == "waiting_window":
            return f"No trade at this time ({self.cfg.trade_windows}); {self._next_window_text(minute)}."
        return ("Scanning: entry when spot breaks the last 5-min candle, all 4 gates pass and the score is at least "
                f"{self.cfg.min_score}/10; {self._next_window_text(minute)}.")

    def _index_line(self, key: str) -> Optional[str]:
        m = self.metrics.get(key)
        if not m:
            return None
        labels = dict(CONDITIONS)
        parts = [f"{key} {m['spot']:,.1f}" if m.get("spot") else key]
        if m.get("triggerUp") is not None:
            parts.append(f"CALL above {m['triggerUp']:,.1f} / PUT below {m['triggerDown']:,.1f}")
        for side in ("CALL", "PUT"):
            s = self.score_summary(m["checks"][side], self.cfg.min_score)
            gates = ("gates OK" if s["gatesOk"] else "gate fail: "
                     + ", ".join(labels.get(g, g).split(" (")[0] for g in s["gatesFailed"]))
            parts.append(f"{side} {gates}, score {s['passed']}/{s['total']} (need {s['need']})"
                         + (" - READY" if s["ready"] else ""))
        return " | ".join(parts)

    def next_actions(self) -> List[str]:
        out = [self.next_action_text()]
        if self.flat_phase() not in ("market_closed",):
            out += [line for line in (self._index_line(k) for k in self.cfg.indices) if line]
        return out

    def _situation_text(self) -> str:
        lines = [f"5-min status ({'PAPER' if self.dry_run else 'LIVE'}): {self.next_action_text()}"]
        for account, engine in self.items.items():
            p = engine._position
            if p is not None:
                lines.append(f"{account}: holding {p.index_key} {int(p.strike)} {'CE' if p.option_type == 'CALL' else 'PE'}"
                             f" x{p.quantity} entry {p.entry_premium} ltp {p.premium} SL {p.stop_spot:,.1f} "
                             f"{'T2' if getattr(p, 't1_done', False) else 'T1'} {p.target_spot:,.1f}")
        lines += [line for line in (self._index_line(k) for k in self.cfg.indices) if line]
        if self._blocked_recent:
            lines.append("Skipped breakouts: " + "; ".join(self._blocked_recent))
        return " || ".join(lines)

    def _take_snapshot(self, now: datetime) -> None:
        self.snapshot = {"time": now.strftime("%H:%M:%S"), "lines": self.next_actions()}
        self._snapshot_holding = self.has_position()

    def snapshot_lines(self) -> List[str]:
        return list(self.snapshot["lines"]) or self.next_actions()

    def _notify_updates(self, now: datetime) -> None:
        """Quiet by design: one situation report per N-min slot (journal + Telegram).

        Entry/exit messages are sent instantly by the engines themselves; here they only
        refresh the "what happens next" snapshot. Nothing else is logged in between.
        """
        if not self.enabled and not self.has_position():
            self._snapshot_holding = None
            self.snapshot = {"time": None, "lines": []}
            self._blocked_recent = []
            return
        if self._snapshot_holding is None or self.has_position() != self._snapshot_holding:
            self._take_snapshot(now)
        every = int(self.cfg.status_report_minutes or 0)
        if every <= 0 or now.minute % every:
            return
        slot = now.strftime("%Y-%m-%d %H:%M")
        if slot == self._last_report_slot:
            return
        self._last_report_slot = slot
        text = self._situation_text()
        self._blocked_recent = []
        self.reports.append({"time": now.strftime("%H:%M"), "text": text})
        self.reports = self.reports[-80:]
        self.history.append(now, "reports", {
            "time": now.isoformat(timespec="seconds"), "mode": "PAPER" if self.dry_run else "LIVE", "text": text,
            "indices": {k: self.history.metrics_row(m) for k, m in self.metrics.items()}})
        self.master._log(text, level="info")
        self._take_snapshot(now)

    def _note_blocked(self, key: str, side: str, bar: float, blockers: List[str], m: Dict[str, Any]) -> None:
        """Remember a blocked breakout for the next 5-min report (no instant message)."""
        if self._blocked_logged.get(key) == (bar, side):
            return
        self._blocked_logged[key] = (bar, side)
        labels = dict(CONDITIONS)
        failing = ", ".join(labels.get(b, b).split(" (")[0] for b in blockers[:4])
        more = f" +{len(blockers) - 4}" if len(blockers) > 4 else ""
        self._blocked_recent.append(f"{key} {side} @ {m.get('spot') or 0:,.1f} (fail: {failing}{more})")
        self._blocked_recent = self._blocked_recent[-6:]
        self.history.breakout(now_ist(), key, side, m, taken=False, blockers=blockers)

    def _data_needed(self, now: datetime) -> bool:
        sess = session_for(now)
        minute = now.hour * 60 + now.minute
        if not sess.trading_day or not sess.open_min <= minute < sess.close_min:
            return False
        return self.enabled or self.has_position()

    async def _signal_loop(self) -> None:
        while self._running:
            try:
                self.cache.schedule(now_ist(), [k for k in self.cfg.indices if get_index(k)])
                await self.tick()
                self.loop_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.loop_error = str(exc)
                logger.exception("Algo 2 signal loop error")
            await asyncio.sleep(SIGNAL_LOOP_SECONDS)

    async def tick(self) -> None:
        now = now_ist()
        if not self._data_needed(now):
            return
        today = now.date()
        indices = [k for k in self.cfg.indices if get_index(k)]
        for key in indices:
            feed.subscribe(get_index(key).sec_id, "IDX_I")
        await self.data.ensure_futures(today)
        await self.news.refresh_nse(HEAVYWEIGHT_IDS.keys(), today)
        await self.data.poll_quotes(indices)
        for key in indices:
            await self.data.refresh_candles(key, now)
            self.data.sample_chain(key, self.data.spot(key), self.cfg.oi_strikes)
            self.metrics[key] = self.compute_metrics(key, now)
            self.history.chain_snapshot(now, key, self.data.spot(key), self.data.futures_price(key),
                                        self.data.chain_expiry(key), self.data.chain_rows(key))
        self.history.on_tick(now, self.metrics)
        self._notify_updates(now)
        if self.has_position() or not self.enabled or self._dispatching:
            return
        if self.trades_done(today.isoformat()):
            return
        if self.sl_hit_today(today.isoformat()):
            return
        for key in indices:
            m = self.metrics.get(key) or {}
            side = m.get("breakout")
            if not side:
                continue
            bar = float((m.get("candle") or {}).get("t") or 0)
            if self._attempted.get(key) == (bar, side):
                continue
            blockers = self.entry_blockers(m, side, self.cfg.min_score)
            if blockers:
                self._note_blocked(key, side, bar, blockers, m)
                continue
            self._attempted[key] = (bar, side)
            await self.dispatch(key, side, m)
            return

    @staticmethod
    def entry_blockers(m: Dict[str, Any], side: str, min_score: Optional[int] = None) -> List[str]:
        """Why ``side`` cannot enter: failed gates, then a score shortfall (empty = entry allowed)."""
        checks = (m.get("checks") or {}).get(side, [])
        s = Algo2Group.score_summary(checks, len(checks) if min_score is None else min_score)
        out = list(s["gatesFailed"])
        if s["passed"] < s["need"]:
            out.append(f"score {s['passed']}/{s['total']} < {s['need']}")
        return out

    # ---------------------------------------------------------- metrics
    def vwap_spot(self, key: str) -> Optional[float]:
        m = self.metrics.get(key) or {}
        vwap, basis = (m.get("vwap") or {}).get("vwap"), m.get("basis")
        if vwap is None:
            return None
        return vwap - (basis or 0.0)

    def compute_metrics(self, key: str, now: datetime) -> Dict[str, Any]:
        cfg = self.cfg
        inst = get_index(key)
        spot = self.data.spot(key)
        rows = self.data.chain_rows(key)
        near = calc.near_rows(rows, spot or 0, cfg.oi_strikes) if rows else []
        net_delta = calc.compute_net_delta(near) if near else None
        delta_series = self.data.delta_series.get(key)
        delta_prev = delta_series.ago(VELOCITY_WINDOW) if delta_series else None
        gex = calc.compute_net_gex(near) if near else None
        pcr = calc.compute_pcr(near) if near else None
        vel = self.data.oi_velocity(key)
        weights = cfg.nifty_weights if key == "NIFTY" else cfg.sensex_weights
        changes = {s: self.data.change_pct("NSE_EQ", HEAVYWEIGHT_IDS[s]) for s in HEAVYWEIGHT_IDS}
        hsmi = calc.compute_hsmi(changes, weights)
        fut_candles = self.data.fut_candles.get(key) or []
        vwap = calc.compute_vwap_bands(fut_candles, cfg.vwap_band_sd)
        profile = calc.compute_market_profile(fut_candles, float(cfg.profile_bin.get(key) or 5.0))
        fut_ltp = self.data.futures_price(key)
        basis = calc.compute_basis(spot, fut_ltp)
        basis_series = self.data.basis_series.get(key)
        basis_prev = basis_series.ago(VELOCITY_WINDOW) if basis_series else None
        vix = self.data.quote("IDX_I", INDIA_VIX_ID)
        vix_move = ((vix["ltp"] - vix["open"]) / vix["open"] * 100.0) if vix and vix.get("open") else None
        candle = self.data.last_closed_candle(key)
        spot_candles = self.data.spot_candles.get(key) or []
        day_high = max((c["high"] for c in spot_candles), default=None)
        day_low = min((c["low"] for c in spot_candles), default=None)
        buffer = float(cfg.entry_buffer.get(key) or 0.0)
        trigger_up = candle["high"] + buffer if candle else None
        trigger_down = candle["low"] - buffer if candle else None
        breakout = None
        if spot and candle:
            breakout = "CALL" if spot > trigger_up else "PUT" if spot < trigger_down else None
        freeze, freeze_name = self.news.is_news_freeze_active(now)
        minute = now.hour * 60 + now.minute
        in_window = self.in_window(minute) and minute < self.master._cutoff_min(now)

        def check(name: str, ok: Optional[bool], value: str) -> Dict[str, Any]:
            gate = name in GATE_CONDITIONS
            return {"key": name, "label": dict(CONDITIONS)[name], "gate": gate,
                    "enabled": gate or bool(cfg.conditions.get(name, True)), "pass": bool(ok), "value": value}

        def fmt(v: Optional[float], digits: int = 2, suffix: str = "") -> str:
            return "n/a" if v is None else f"{v:,.{digits}f}{suffix}"

        checks: Dict[str, List[Dict[str, Any]]] = {}
        for side in ("CALL", "PUT"):
            sign = 1 if side == "CALL" else -1
            same, other = ("ce", "pe") if side == "CALL" else ("pe", "ce")
            trio = [changes.get(s) for s in TRIO]
            weighted_against = sum(w for s, w in weights.items()
                                   if changes.get(s) is not None and sign * changes[s] < 0)
            weighted_known = sum(w for s, w in weights.items() if changes.get(s) is not None)
            band = (vwap or {}).get("upper" if side == "CALL" else "lower")
            area = (profile or {}).get("vah" if side == "CALL" else "val")
            new_extreme = bool(spot and day_high and spot > day_high) if side == "CALL" else bool(
                spot and day_low and spot < day_low)
            checks[side] = [
                check("news", not freeze, freeze_name or "clear"),
                check("time_window", in_window, cfg.trade_windows),
                check("pcr_filter", pcr is not None and not cfg.pcr_chop_low <= pcr <= cfg.pcr_chop_high,
                      fmt(pcr)),
                check("net_delta", net_delta is not None and delta_prev is not None
                      and sign * (net_delta - delta_prev) > 0, f"{fmt(net_delta, 0)} vs {fmt(delta_prev, 0)}"),
                check("gex_negative", gex is not None and gex < 0, fmt(gex, 0)),
                check("oi_velocity", calc.oi_velocity_spike(vel, other, cfg.oi_velocity_spike),
                      f"{other.upper()} {fmt(vel.get(other), 1, '%')}"),
                check("oi_unwinding", vel.get(same) is not None and vel[same] < 0,
                      f"{same.upper()} {fmt(vel.get(same), 1, '%')}"),
                check("hsmi", hsmi is not None and sign * hsmi > 0, fmt(hsmi, 3, "%")),
                check("trio_sync", all(v is not None and sign * v > 0 for v in trio),
                      " ".join(f"{s[:4]} {fmt(changes.get(s), 2, '%')}" for s in TRIO)),
                check("heavyweight_divergence", weighted_known > 0 and weighted_against <= weighted_known / 2,
                      f"{weighted_against / weighted_known * 100:.0f}% weight against" if weighted_known else "n/a"),
                check("vwap_band", fut_ltp is not None and band is not None and sign * (fut_ltp - band) > 0,
                      f"fut {fmt(fut_ltp, 1)} vs {fmt(band, 1)}"),
                check("market_profile", fut_ltp is not None and area is not None and sign * (fut_ltp - area) > 0,
                      f"fut {fmt(fut_ltp, 1)} vs {'VAH' if side == 'CALL' else 'VAL'} {fmt(area, 1)}"),
                check("basis", basis is not None and (
                    (basis >= 0 and not calc.basis_divergence(new_extreme, basis)) if side == "CALL"
                    else basis_prev is not None and basis <= basis_prev),
                    f"{fmt(basis, 1)} (5m ago {fmt(basis_prev, 1)})"),
                check("vix", vix_move is not None and (vix_move < cfg.vix_spike_pct if side == "CALL"
                                                       else vix_move > -cfg.vix_spike_pct),
                      fmt(vix_move, 2, "%")),
            ]
        score = {s: self.score_summary(checks[s], cfg.min_score) for s in checks}
        return {
            "index": key, "name": inst.name if inst else key, "spot": spot,
            "candle": candle, "triggerUp": trigger_up, "triggerDown": trigger_down, "breakout": breakout,
            "netDelta": net_delta, "netDeltaPrev": delta_prev, "netGex": gex, "pcr": pcr,
            "oiVelocity": vel, "hsmi": hsmi, "heavyweights": changes, "vwap": vwap, "profile": profile,
            "futures": fut_ltp, "basis": basis, "basisPrev": basis_prev, "vixMovePct": vix_move,
            "chainRows": len(rows), "candles": len(spot_candles), "futCandles": len(fut_candles),
            "checks": checks, "score": score,
            "allPass": {s: score[s]["ready"] for s in checks},
        }

    @staticmethod
    def score_summary(checks: List[Dict[str, Any]], min_score: int) -> Dict[str, Any]:
        gates = [c for c in checks if c.get("gate")]
        scored = [c for c in checks if not c.get("gate") and c["enabled"]]
        passed = sum(1 for c in scored if c["pass"])
        need = min(int(min_score), len(scored))
        gates_ok = all(c["pass"] for c in gates)
        return {"gatesOk": gates_ok, "gatesFailed": [c["key"] for c in gates if not c["pass"]],
                "passed": passed, "total": len(scored), "need": need,
                "ready": gates_ok and passed >= need}

    # --------------------------------------------------------- dispatch
    async def dispatch(self, key: str, side: str, m: Dict[str, Any]) -> None:
        """Send one signal to every selected account (mirrors Algo 1's shared-signal flow)."""
        self._dispatching = True
        try:
            await self._dispatch(key, side, m)
        finally:
            self._dispatching = False

    async def _dispatch(self, key: str, side: str, m: Dict[str, Any]) -> None:
        master = self.master
        candle = m["candle"]
        spot = m["spot"]
        leg = DayLeg(key, status="watching", or_high=round(m["triggerUp"], 2), or_low=round(m["triggerDown"], 2),
                     spot=round(spot, 2))
        rows = self.data.chain_rows(key)
        wall_strike = calc.oi_wall(rows, spot, side, self.cfg.oi_strikes * 2)
        self.signal_ctx[key] = {"candleHigh": candle["high"], "candleLow": candle["low"], "wall": wall_strike}
        with account_scope("master"):
            await master._prepare_contract(leg, side)
        if leg.prepared_side != side or not leg.security_id:
            master._log(f"{key} {side} signal skipped: {leg.note or 'contract not ready'}", level="warn", index=key)
            return
        segment = master._segment_for(key)
        age = feed.age(leg.security_id, segment)
        if feed.price(leg.security_id, segment) is None or age is None or age > MAX_PRICE_AGE:
            self._attempted.pop(key, None)   # premium tick not here yet: retry next second
            return
        today = now_ist().date().isoformat()
        trade_no = self.signals_used(today) + 1
        s = self.score_summary(m["checks"][side], self.cfg.min_score)
        passed = ", ".join(c["key"] for c in m["checks"][side] if c["enabled"] and c["pass"] and not c.get("gate"))
        self.history.breakout(now_ist(), key, side, m, taken=True, blockers=[])
        limit = f"/{self.cfg.max_trades}" if self.cfg.trade_limit else ""
        master._log(f"SIGNAL {key} {side} (trade {trade_no}{limit}): spot {spot:,.1f} broke 5-min "
                    f"candle {'high' if side == 'CALL' else 'low'} {leg.or_high if side == 'CALL' else leg.or_low:,.1f}; "
                    f"T1 wall {wall_strike or '1:2 R:R'}; gates OK, score {s['passed']}/{s['total']} "
                    f"(need {s['need']}) [{passed}] -> "
                    f"{','.join(self.entry_accounts)}", level="success", index=key)
        signal = asdict(leg)
        consumed = False

        async def enter(account: AccountId) -> None:
            nonlocal consumed
            engine = self.items[account]
            with account_scope(account):
                try:
                    async with engine._execution_lock:
                        if not engine.enabled or engine._position is not None:
                            return
                        local = DayLeg(**signal)
                        engine._legs[key] = local
                        engine._signal_t0 = time.perf_counter()
                        await engine._enter(local, side)
                        if (engine._position is None and local.status == "skipped"
                                and "insufficient balance" in (local.note or "")
                                and await engine.free_margin_from_algo1(key)):
                            consumed = True
                            for _ in range(PRIORITY_ENTRY_TRIES):
                                # Already-accepted signal: re-send without a breakout recheck.
                                local.status, local.note, local.funds_retries = "watching", "", 1
                                await engine._enter(local, side)
                                if engine._position is not None or "insufficient balance" not in (local.note or ""):
                                    break
                                await asyncio.sleep(PRIORITY_POLL_SECONDS)
                        if engine._position is not None or local.entry_attempts > 0:
                            consumed = True
                        elif local.note:
                            engine._log(f"{key} {side} not entered: {local.note}", level="warn", index=key)
                except Exception as exc:  # noqa: BLE001
                    consumed = True
                    engine._last_error = str(exc)
                    engine._log(f"Algo 2 signal failed: {exc}", level="error")
                    logger.exception("account=%s algo2 signal failed", account)
                finally:
                    try:
                        engine._persist()
                    except OSError:
                        logger.exception("account=%s algo2 persist failed", account)

        await asyncio.gather(*(enter(a) for a in self.entry_accounts))
        self.last_signal = {"index": key, "side": side, "spot": spot, "at": now_ist().strftime("%H:%M:%S"),
                            "consumed": consumed, "wall": wall_strike}
        if consumed:
            self.signal_day, self.signal_count = today, trade_no
            self.save_settings()

    # ----------------------------------------------------------- status
    def status(self) -> Dict[str, Any]:
        now = now_ist()
        accounts = {}
        for account, engine in self.items.items():
            with account_scope(account):
                st = engine.status()
            accounts[account] = {k: st.get(k) for k in (
                "enabled", "phase", "phaseLabel", "position", "nextActions", "todayTrades", "stats",
                "lastError", "brokerSyncAt", "brokerSyncError", "brokerNetQty", "brokerPositionPnl",
                "paperBalance", "events")}
            accounts[account]["configured"] = account == "master" or all(account_credentials(account))
            accounts[account]["selected"] = account in self.entry_accounts
        return {
            "algo": "Algo 2",
            "enabled": self.enabled,
            "dryRun": self.dry_run,
            "entryAccounts": list(self.entry_accounts),
            "serverTime": now.strftime("%Y-%m-%d %H:%M:%S"),
            "phase": self.master.phase if self.master.enabled or self.master._position else (
                self.flat_phase() if self.enabled else "off"),
            "config": asdict(self.cfg),
            "conditionCatalog": [{"key": k, "label": label, "gate": k in GATE_CONDITIONS} for k, label in CONDITIONS],
            "metrics": self.metrics,
            "news": self.news.status(now),
            "data": self.data.status(),
            "dailyCache": self.cache.status(),
            "signals": {"day": self.signal_day, "count": self.signals_used(now.date().isoformat()),
                        "max": self.cfg.max_trades if self.cfg.trade_limit else None,
                        "last": self.last_signal},
            "loopError": self.loop_error,
            "nextActions": self.snapshot_lines(),
            "nextActionsAt": self.snapshot["time"],
            "reports": list(reversed(self.reports[-30:])),
            "slStop": self.sl_hit_today(now.date().isoformat()),
            "accounts": accounts,
        }


group = Algo2Group()
