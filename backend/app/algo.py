"""ORB Algo — the automatic Opening-Range Breakout trader.

What it does, once you flip it ON
---------------------------------
  09:15-09:24:59 builds the opening range (OR high / OR low) for NIFTY + SENSEX
  09:25        locks the levels, pre-picks the option contract, sizes the lots
  after 09:25  watches BOTH indices tick-by-tick. The FIRST one to break its
               range takes the trade; the other is locked out for that trade.
  after exit   up to max_trades (default 2) per day: once EVERY account is
               flat, both indices are re-watched with the same OR. A side spot
               is already beyond needs a fresh breakout (back inside first).
               break UP   -> buy CALL (ATM, or ATM +/- offset)
               break DOWN -> buy PUT
  in trade     target = entry spot +/- (target_mult x OR range)
               target-only default: spot SL and break-even OFF
               broker premium SL = 30% of entry premium (70% away)
               classic spot stops require explicit target_only=False
  15:29        force square-off (MARGIN product, so Dhan does not auto-square ~15:18)
  15:30        day closed, everything archived to the journal

Safety model (read this)
------------------------
* DRY-RUN is the default. In dry-run NOTHING is sent to Dhan; fills are
  simulated from live prices so you can paper-trade the real signals.
* Software spot targets depend on this process and fresh quotes. Target-only
  has no tight software stop. The far broker STOP_LOSS_MARKET SELL can still
  hit, fail or slip; losses can approach the whole premium. Classic study
  statistics do not validate target-only behavior.
* State is persisted to disk after every meaningful change and re-synced with
  Dhan's own position book on startup, so a crash/restart resumes mid-day.
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from . import audit, runtime
from .algo_store import store
from .config import get_settings
from .dhan_client import DhanAPIError, get_trading_client as get_dhan_client, get_dhan_client as get_data_client
from .accounts import ACCOUNT_IDS, AccountId, account_credentials, account_scope, current_account
from .feed import feed
from .instruments import get_index, lot_size_for
from .optionchain import engine as chain_engine
from .opening_range import INTRADAY_FROM_TIME, WINDOW_END_MIN, WINDOW_START_MIN
from .orders import _round_tick

logger = logging.getLogger("dhan.algo")

IST = timezone(timedelta(hours=5, minutes=30))
MARKET_OPEN_MIN = 9 * 60 + 15
MARKET_CLOSE_MIN = 15 * 60 + 30
OR_READY_MIN = 9 * 60 + 25          # start computing the OR at/after this minute

# One-off exchange sessions (e.g. Diwali Muhurat). Same ORB rules, shifted clock.
# open = first CTS minute; OR = open..open+9 (10 candles); lock at open+10.
SPECIAL_SESSIONS: Dict[str, Dict[str, Any]] = {
    "2026-11-08": {"name": "Muhurat", "open": 14 * 60 + 20, "close": 15 * 60 + 40,
                   "entry_cutoff": "15:15", "exit_time": "15:30", "max_trades": 1},
}


@dataclass(frozen=True)
class Session:
    trading_day: bool
    open_min: int
    or_end_min: int
    or_ready_min: int
    close_min: int
    special: Optional[Dict[str, Any]] = None


def session_for(now: datetime) -> Session:
    sp = SPECIAL_SESSIONS.get(now.date().isoformat())
    if sp:
        o = int(sp["open"])
        return Session(True, o, o + 9, o + 10, int(sp["close"]), sp)
    return Session(now.weekday() < 5, MARKET_OPEN_MIN, WINDOW_END_MIN, OR_READY_MIN, MARKET_CLOSE_MIN)


def _hhmm(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


TICKER = 15
RECONCILE_SECONDS = 1.0
MAX_PRICE_AGE = 5.0
# Pause before re-checking a breakout whose entry aborted before any order was sent.
SIGNAL_RETRY_COOLDOWN = 3.0
# Max-affordable sizing keeps this share of balance usable; the rest covers charges and MARKET slippage.
AFFORDABLE_BALANCE_PCT = 90.0   # 10% buffer for premium move + Dhan market-order margin
TERMINAL_ORDERS = {"TRADED", "REJECTED", "CANCELLED", "EXPIRED"}
# Algo's broker-parked disaster SL: trigger when premium has fallen this % from entry.
ALGO_PROTECTIVE_SL_PCT = 70.0
# A funds/margin rejection is retried at once with one lot fewer, at most this many times.
FUNDS_RETRY_MAX = 3
# A funds rejection discovered later than this after the BUY is too stale to chase.
FUNDS_RETRY_WINDOW = 120.0
_FUNDS_REJECT = re.compile(r"insufficient|fund|margin|RMS", re.IGNORECASE)

# Order-placement retry policy. `_evaluate` runs on EVERY feed tick, so without a
# cap+backoff a rejected entry is retried many times per second. On 2026-09-28 a
# DH-905 (invalid IP) rejection produced 586 order calls in 2.5 minutes.
ENTRY_MAX_ATTEMPTS = 3
ENTRY_RETRY_BACKOFF = (2.0, 5.0, 15.0)   # seconds, indexed by attempt-1
# Errors that will NOT fix themselves during the session. Retrying these just
# hammers Dhan and risks an API block, so we stop and disarm instead.
FATAL_ORDER_CODES = {
    "DH-901",  # invalid/expired token
    "DH-902",  # no Trading API access
    "DH-903",  # account/segment issue
    "DH-905",  # input exception - in practice "Invalid IP" (static IP not set)
}


def now_ist() -> datetime:
    return datetime.now(IST)


def _to_min(hhmm: str, default: int) -> int:
    try:
        h, m = hhmm.split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return default


def _fmt_min(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


# --------------------------------------------------------------------------- #
#  Config / state containers
# --------------------------------------------------------------------------- #
@dataclass
class AlgoConfig:
    indices: List[str] = field(default_factory=lambda: ["NIFTY", "SENSEX"])
    target_only: bool = True       # no software spot stop/break-even; broker SL 70% away
    sl_mult: float = 0.2            # stop   = sl_mult     x OR range
    target_mult: float = 2.0        # target = target_mult x OR range
    breakeven_at: float = 0.0       # move stop to entry after this many x OR (0=off)
    entry_cutoff: str = "15:00"     # no new entries after this IST time
    exit_time: str = "15:29"        # forced square-off
    max_trades: int = 2             # ORB signals per day (next one only after every account is flat)
    strike_offset: int = 0          # 0 = ATM, +1 = one strike further OTM
    risk_pct: float = 0.5           # % of balance risked per trade (see Settings)
    max_capital_pct: float = 60.0   # max % of balance spent on premium
    max_lots: int = 10
    min_lots: int = 1
    min_or_pct: float = 0.0         # skip if OR range < this % of spot (0 = off)
    max_or_pct: float = 0.0         # skip if OR range > this % of spot (0 = off)
    order_type: str = "MARKET"
    product_type: str = "MARGIN"
    protective_sl: bool = True      # park a wide real SL at Dhan as a disaster stop
    protective_sl_mult: float = 2.0 # ... at this many x the software stop distance
    # Size with every lot the balance can buy (capped by max_lots), ignoring risk_pct/max_capital_pct.
    max_affordable: bool = False
    # Per-account rupee exits on live premium P&L (0 = off). Editable while armed/in a position.
    profit_target_rs: float = 0.0
    stop_loss_rs: float = 0.0

    @classmethod
    def from_settings(cls) -> "AlgoConfig":
        s = get_settings()
        return cls(
            indices=s.algo_indices or ["NIFTY", "SENSEX"],
            target_only=s.algo_target_only,
            sl_mult=s.algo_sl_mult,
            target_mult=s.algo_target_mult,
            breakeven_at=s.algo_breakeven_at,
            entry_cutoff=s.algo_entry_cutoff,
            exit_time=s.algo_exit_time,
            max_trades=s.algo_max_trades,
            strike_offset=s.algo_strike_offset,
            risk_pct=s.algo_effective_risk_pct,
            max_capital_pct=s.algo_max_capital_pct,
            max_lots=s.algo_max_lots,
            min_lots=s.algo_min_lots,
            min_or_pct=s.algo_min_or_pct,
            max_or_pct=s.algo_max_or_pct,
            order_type=s.algo_order_type,
            product_type=s.algo_product_type,
            max_affordable=s.algo_max_affordable,
            profit_target_rs=s.algo_profit_target_rs,
            stop_loss_rs=s.algo_stop_loss_rs,
        )


@dataclass
class DayLeg:
    """One index's plan/state for today."""

    index_key: str
    status: str = "preopen"     # preopen|watching|active|locked|skipped|closed
    or_high: Optional[float] = None
    or_low: Optional[float] = None
    spot: Optional[float] = None
    signal: Optional[str] = None            # CALL | PUT
    strike: Optional[float] = None
    security_id: Optional[int] = None
    expiry: Optional[str] = None
    option_ltp: Optional[float] = None
    planned_lots: Optional[int] = None
    note: str = ""
    prepared_side: str = ""        # CALL/PUT of the currently held security_id
    entry_attempts: int = 0
    next_try_at: float = 0.0        # epoch seconds; entry retried only after this
    error: str = ""
    lot_cap: Optional[int] = None   # set after a funds rejection: retry with fewer lots
    funds_retries: int = 0
    # Next-trade re-watch: a side spot is already beyond needs a fresh breakout.
    rearm: bool = False
    blocked_side: str = ""

    @property
    def or_range(self) -> Optional[float]:
        if self.or_high is None or self.or_low is None:
            return None
        return round(self.or_high - self.or_low, 2)


@dataclass
class Position:
    index_key: str
    option_type: str            # CALL | PUT
    security_id: int
    strike: float
    expiry: str
    lots: int
    quantity: int
    entry_spot: float
    entry_premium: float
    entry_time: str
    entry_ts: float
    or_range: float
    target_spot: float
    stop_spot: float
    initial_stop_spot: float
    breakeven_done: bool = False
    order_id: Optional[str] = None
    protective_order_id: Optional[str] = None
    dry_run: bool = True
    # live
    spot: Optional[float] = None
    premium: Optional[float] = None
    mfe: float = 0.0
    mae: float = 0.0
    execution_state: str = "open"  # entry_pending | open | exit_pending | attention
    entry_correlation_id: Optional[str] = None
    exit_order_id: Optional[str] = None
    exit_correlation_id: Optional[str] = None
    protective_correlation_id: Optional[str] = None
    exit_reason: str = ""
    execution_error: str = ""
    next_exit_try_at: float = 0.0
    entry_confirmed_ts: float = 0.0
    broker_exit_premium: Optional[float] = None
    entry_quantity: int = 0
    entry_cancel_requested: bool = False
    partial_exit: bool = False
    protective_cancel_requested: bool = False
    protective_policy_pending: bool = False

    @property
    def risk_points(self) -> float:
        return abs(self.entry_spot - self.initial_stop_spot)

    @property
    def pnl_points(self) -> float:
        if self.spot is None:
            return 0.0
        d = self.spot - self.entry_spot
        return d if self.option_type == "CALL" else -d

    @property
    def pnl_premium(self) -> float:
        if self.premium is None:
            return 0.0
        return (self.premium - self.entry_premium) * self.quantity

    @property
    def r_multiple(self) -> float:
        return self.pnl_points / self.risk_points if self.risk_points else 0.0


# --------------------------------------------------------------------------- #
#  Engine
# --------------------------------------------------------------------------- #
class AlgoEngine:
    ALGO_LABEL = "Algo 1"
    ORDER_TAG = "ORB"   # correlation-id prefix of this algo's orders
    POSITION_CLS = Position

    def _is_dry(self) -> bool:
        return runtime.is_dry_run()

    @property
    def _store(self):
        return store

    @property
    def _group(self) -> "AccountEngines":
        return engines

    def _levels(self, leg: DayLeg, direction: str, spot: float, rng: float,
                sizing: Dict[str, Any]) -> tuple:
        """(target_spot, stop_spot) for a new entry."""
        sign = 1 if direction == "CALL" else -1
        return spot + sign * self.cfg.target_mult * rng, spot - sign * self.cfg.sl_mult * rng

    def __init__(self, account_id: Optional[AccountId] = None) -> None:
        self.account_id = account_id
        self.cfg = AlgoConfig.from_settings()
        self.enabled = False
        self.phase = "off"
        self._day: Optional[str] = None
        self._legs: Dict[str, DayLeg] = {}
        # Backup opening range built from live index ticks, used only if /charts/intraday fails.
        self._tick_or: Dict[str, Dict[str, Any]] = {}
        self._position: Optional[Position] = None
        self._events: List[Dict[str, Any]] = []
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._busy = False
        self._evaluation_task: Optional[asyncio.Task] = None
        self._execution_lock = asyncio.Lock()
        self._last_reconcile_at = 0.0
        self._broker_sync_at: Optional[str] = None
        self._broker_sync_error: Optional[str] = None
        self._broker_net_qty: Optional[int] = None
        self._broker_position_pnl: Optional[float] = None
        self._started_at = time.time()
        self._feed_connected = False
        self._last_error: Optional[str] = None
        self._today_trades: List[Dict[str, Any]] = []
        self._recovered: Optional[str] = None
        self._signal_day: Optional[str] = None
        self._signal_count = 0

    def _signals_used(self, today: str) -> int:
        if self._signal_day != today:
            return 0
        return max(1, self._signal_count)

    def _sess_val(self, key: str, now: Optional[datetime] = None) -> Any:
        sp = session_for(now or now_ist()).special
        return sp[key] if sp and key in sp else getattr(self.cfg, key)

    def _max_trades(self, now: Optional[datetime] = None) -> int:
        return int(self._sess_val("max_trades", now))

    def _exit_min(self, now: Optional[datetime] = None) -> int:
        return _to_min(self._sess_val("exit_time", now), 15 * 60 + 12)

    def _cutoff_min(self, now: Optional[datetime] = None) -> int:
        return _to_min(self._sess_val("entry_cutoff", now), 15 * 60)

    # ------------------------------------------------------------------ log
    def _log(self, msg: str, level: str = "info", notification: str = "algo_update", **extra: Any) -> None:
        msg = f"account={self.account_id or current_account()} {msg}"
        ev = {"kind": "algo", "level": level, "msg": msg, **extra}
        self._events.append(ev)
        if len(self._events) > 400:
            self._events = self._events[-400:]
        self._store.append_event(ev, day=self._day)
        getattr(logger, {"warn": "warning"}.get(level, level), logger.info)(msg, extra={
            "telegram_event": notification,
            "telegram_account": self.account_id or current_account(),
            "telegram_mode": "DRY-RUN" if self._is_dry() else "LIVE",
            "telegram_detail": f"{self.ALGO_LABEL} | {msg.split(' ', 1)[-1]}",
            "telegram_level": level,
        })

    # ------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        # Rolling retention: drop journals older than 7 days.
        self._store.prune_journals()
        if self.account_id is None:
            feed.set_on_tick(self._on_tick)
            await feed.start()
        self._restore()
        self._subscribe_tracked()
        self._task = asyncio.create_task(self._supervisor(), name="algo-supervisor")
        logger.info("Algo engine started (enabled=%s, dryRun=%s)", self.enabled, self._is_dry())

    async def stop(self) -> None:
        self._running = False
        if self._evaluation_task:
            self._evaluation_task.cancel()
            try:
                await self._evaluation_task
            except asyncio.CancelledError:
                pass
            self._evaluation_task = None
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        if self.account_id is None:
            await feed.stop()
        self._persist()

    # ---------------------------------------------------------------- state
    def _persist(self) -> None:
        self._store.save_state(
            {
                "enabled": self.enabled,
                "phase": self.phase,
                "day": self._day,
                "config": asdict(self.cfg),
                "legs": {k: asdict(v) for k, v in self._legs.items()},
                "position": asdict(self._position) if self._position else None,
                "todayTrades": self._today_trades,
                "savedAt": time.time(),
                "signalDay": self._signal_day,
                "signalCount": self._signal_count,
                "accountId": self.account_id or current_account(),
                "clientId": account_credentials(self.account_id)[0] if self.account_id else None,
            }
        )

    def _restore(self) -> None:
        """Reload the previous session and re-sync with Dhan's position book."""
        data = self._store.load_state()
        if not data:
            return
        if self.account_id and data.get("clientId") and data["clientId"] != account_credentials(self.account_id)[0]:
            raise RuntimeError(f"{self.account_id}: saved trading state belongs to a different Dhan client ID. "
                               "Restore the original credentials and reconcile before changing accounts.")
        try:
            cfg = type(self.cfg)(**{**asdict(self.cfg), **(data.get("config") or {})})
            self.cfg = cfg
        except TypeError:
            pass
        self.enabled = bool(data.get("enabled"))
        self._signal_day = data.get("signalDay")
        self._signal_count = int(data.get("signalCount") or (1 if self._signal_day else 0))
        self._day = data.get("day")
        self._today_trades = list(data.get("todayTrades") or [])
        legs = data.get("legs") or {}
        for key in self.cfg.indices:
            if key in legs:
                try:
                    self._legs[key] = DayLeg(**legs[key])
                except TypeError:
                    self._legs[key] = DayLeg(key)
        pos = data.get("position")
        if pos:
            try:
                self._position = self.POSITION_CLS(**pos)
                if "target_only" not in (data.get("config") or {}):
                    self._position.protective_policy_pending = self.cfg.target_only
                    if not self._position.exit_order_id and not self._position.exit_correlation_id:
                        self._position.exit_reason = ""
                    self._log("Migrating legacy position to target-only: software SL disabled; old broker SL must be verified/cancelled before replacement.",
                              level="warn")
            except TypeError:
                self._position = None
        if self._day:
            self._recovered = (
                f"Recovered session from disk (day {self._day}, phase "
                f"{data.get('phase')}, position={'yes' if self._position else 'no'})"
            )
            logger.info(self._recovered)

    async def reconcile(self) -> Dict[str, Any]:
        """Compare our idea of the world with Dhan's actual position book."""
        async with self._execution_lock:
            return await self._reconcile_locked()

    def _subscribe_tracked(self) -> None:
        for key in self.cfg.indices:
            inst = get_index(key)
            if inst:
                feed.subscribe(inst.sec_id, "IDX_I")
        if self._position:
            feed.subscribe(self._position.security_id, self._segment_for(self._position.index_key))

    async def _order_state(self, order_id: Optional[str], correlation: Optional[str]) -> Dict[str, Any]:
        client = get_dhan_client()
        if order_id:
            raw = await client.get_order_by_id(order_id)
        elif correlation:
            raw = await client.get_order_by_correlation(correlation)
        else:
            raise DhanAPIError(0, "UNKNOWN_ORDER", "Missing order identity; check Dhan before retrying.")
        if isinstance(raw, list):
            matches = [row for row in raw if isinstance(row, dict) and (
                (order_id and str(row.get("orderId")) == str(order_id))
                or (not order_id and correlation and row.get("correlationId") == correlation)
            )]
            raw = matches[0] if len(matches) == 1 else None
        if not isinstance(raw, dict) or not raw.get("orderStatus"):
            raise DhanAPIError(0, "UNKNOWN_ORDER", "Broker returned no order status; check Dhan.")
        return raw

    async def _recover_historical_flat(
        self, pos: Position, rows: Optional[List[Dict[str, Any]]] = None,
    ) -> bool:
        """Retire prior-session DAY orders only after fresh books and flat holdings."""
        today = now_ist().date().isoformat()
        if (self._position is not pos or pos.dry_run or not self._day
                or self._day >= today or self.cfg.product_type != "INTRADAY"):
            return False

        def matches(row: Dict[str, Any]) -> bool:
            return (str(row.get("securityId")) == str(pos.security_id)
                    and row.get("exchangeSegment") == self._segment_for(pos.index_key)
                    and row.get("productType") == self.cfg.product_type)

        client = get_dhan_client()
        try:
            if rows is None:
                rows = await client.get_positions(fresh=True)
            if not isinstance(rows, list):
                raise DhanAPIError(0, "BAD_POSITIONS", "Invalid broker positions response")
            if any(matches(row) and int(row.get("netQty") or 0) != 0 for row in rows):
                return False
            book = await client.get_order_book(fresh=True)
            supers = await client.get_super_orders(fresh=True)
            if not isinstance(book, list) or not isinstance(supers, list):
                raise DhanAPIError(0, "BAD_ORDERS", "Cannot verify current broker order books")
            identities = {str(identity) for identity in (
                pos.order_id, pos.protective_order_id, pos.exit_order_id,
            ) if identity}
            for order in [*book, *supers]:
                if not matches(order) and str(order.get("orderId")) not in identities:
                    continue
                status = str(order.get("orderStatus") or "").upper()
                if status not in TERMINAL_ORDERS | {"CLOSED"}:
                    pos.execution_error = "Historical recovery blocked: a current contract order is pending/unknown at Dhan."
                    self._persist()
                    return False
                if any(str(leg.get("orderStatus") or "").upper() not in TERMINAL_ORDERS
                       for leg in order.get("legDetails") or []):
                    pos.execution_error = "Historical recovery blocked: a Super Order leg may still execute."
                    self._persist()
                    return False
            fresh = await client.get_positions(fresh=True)
            if not isinstance(fresh, list):
                raise DhanAPIError(0, "BAD_POSITIONS", "Invalid broker positions recheck")
            if any(matches(row) and int(row.get("netQty") or 0) != 0 for row in fresh):
                return False
        except DhanAPIError as exc:
            self._broker_sync_error = exc.error_message
            pos.execution_error = f"Historical recovery not confirmed: {exc.error_message}. Check Dhan."
            self._log(pos.execution_error, level="warn")
            self._persist()
            return False
        self._broker_sync_at = now_ist().isoformat()
        self._broker_sync_error = None
        self._broker_net_qty = 0
        if pos.execution_state == "entry_pending":
            self._log(
                f"Historical entry {pos.order_id or pos.entry_correlation_id}: broker flat twice, "
                "no actionable current orders. Tracking retired; historical fill/P&L UNKNOWN, no order sent.",
                level="warn", outcome="HISTORICAL_ENTRY_FLAT", fillKnown=False,
            )
            self._position = None
            self.enabled = False
            self._last_error = None
            self.phase = "off"
            self._persist()
            return True
        self._log(
            f"Recovered stale {self._day} INTRADAY tracking: broker flat twice and no active "
            "current orders/legs. Prior-session DAY orders are no longer actionable; no order sent.",
            level="warn",
        )
        self._finalise_position(
            pos, pos.broker_exit_premium or pos.premium or pos.entry_premium,
            "BROKER_FLAT_RECOVERED", estimated=True,
        )
        self._last_error = None
        return True

    async def _reconcile_locked(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {"checkedAt": now_ist().strftime("%H:%M:%S")}
        self._last_reconcile_at = time.monotonic()
        pos = self._position
        if pos is not None and pos.dry_run:
            self._broker_net_qty = None
            if pos.protective_policy_pending:
                await self._migrate_protective_policy(pos)
            result["skipped"] = "Paper position is not compared with live holdings"
            return result
        if not get_settings().is_configured():
            result["skipped"] = "Dhan not configured"
            return result
        try:
            self._broker_net_qty = None
            self._broker_position_pnl = None
            if pos is not None and pos.execution_state == "entry_pending":
                if self._day and self._day < now_ist().date().isoformat():
                    if await self._recover_historical_flat(pos):
                        return result
                await self._refresh_entry(pos)
                if self._position is None:
                    await self._retry_after_delayed_reject(pos)
                    return result
                if pos.execution_state == "entry_pending":
                    return result
            rows = await get_dhan_client().get_positions(fresh=True)
            if not isinstance(rows, list):
                raise DhanAPIError(0, "BAD_POSITIONS", "Invalid broker positions response")
            self._broker_sync_at = now_ist().isoformat()
            self._broker_sync_error = None
        except DhanAPIError as exc:
            result["error"] = exc.error_message
            self._last_error = exc.error_message
            self._broker_sync_error = exc.error_message
            logger.warning("Algo broker sync failed: %s", exc)
            return result
        net: Dict[str, int] = {}
        for r in rows or []:
            if pos and (r.get("exchangeSegment") != self._segment_for(pos.index_key)
                        or r.get("productType") != self.cfg.product_type):
                continue
            sid = str(r.get("securityId") or "")
            net[sid] = net.get(sid, 0) + int(r.get("netQty") or 0)
        result["openPositions"] = {k: v for k, v in net.items() if v}
        if pos is not None:
            held = net.get(str(pos.security_id), 0)
            self._broker_net_qty = held
            matching_rows = [
                row for row in rows if str(row.get("securityId")) == str(pos.security_id)
                and row.get("exchangeSegment") == self._segment_for(pos.index_key)
                and row.get("productType") == self.cfg.product_type
            ]
            if matching_rows and all(
                row.get("realizedProfit") is not None and row.get("unrealizedProfit") is not None
                for row in matching_rows
            ):
                try:
                    pnl = sum(float(row["realizedProfit"]) + float(row["unrealizedProfit"]) for row in matching_rows)
                    if not math.isfinite(pnl):
                        raise ValueError("P&L is not finite")
                    self._broker_position_pnl = pnl
                except (TypeError, ValueError):
                    self._broker_sync_error = "Invalid broker contract P&L; holding quantity remains authoritative."
                    logger.warning("account=%s %s", self.account_id, self._broker_sync_error)
            if held > 0 and pos.protective_policy_pending:
                if not await self._migrate_protective_policy(pos):
                    return result
                rows = await get_dhan_client().get_positions(fresh=True)
                held = sum(int(r.get("netQty") or 0) for r in rows
                           if str(r.get("securityId")) == str(pos.security_id)
                           and r.get("exchangeSegment") == self._segment_for(pos.index_key)
                           and r.get("productType") == self.cfg.product_type)
                net[str(pos.security_id)] = held
            if held == 0:
                if pos.entry_confirmed_ts and time.time() - pos.entry_confirmed_ts < RECONCILE_SECONDS:
                    return result
                if self._day and self._day < now_ist().date().isoformat() and self.cfg.product_type == "INTRADAY":
                    result["historicalRecovered"] = await self._recover_historical_flat(pos, rows)
                    return result
                pos.execution_state = "attention"
                pos.execution_error = "Broker confirms zero holding; verifying outstanding exits/protection. No repeat SELL."
                # Never clear a position while any SELL may still execute.
                if not await self._cancel_protective(pos):
                    return result
                exit_price = pos.broker_exit_premium
                if pos.exit_order_id or pos.exit_correlation_id:
                    try:
                        order = await self._order_state(pos.exit_order_id, pos.exit_correlation_id)
                        if str(order["orderStatus"]).upper() not in TERMINAL_ORDERS:
                            pos.execution_error = "Exit still pending at broker; no duplicate SELL will be sent."
                            self._persist()
                            return result
                        exit_price = _as_float(order.get("averageTradedPrice")) or exit_price
                    except DhanAPIError as exc:
                        self._broker_sync_error = exc.error_message
                        return result
                if not exit_price or pos.partial_exit:
                    matching = [r for r in rows if str(r.get("securityId")) == str(pos.security_id)
                                and r.get("exchangeSegment") == self._segment_for(pos.index_key)
                                and r.get("productType") == self.cfg.product_type]
                    exit_price = next((_as_float(r.get("sellAvg")) for r in matching if r.get("sellAvg")), None) or exit_price
                self._log(
                    f"Reconcile: {pos.index_key} {pos.option_type} no longer held at Dhan "
                    f"-> marking trade closed", level="warn")
                self._finalise_position(
                    pos, exit_price or pos.premium or pos.entry_premium,
                    pos.exit_reason or "BROKER_CLOSED", estimated=not bool(exit_price),
                )
            elif held < 0 or held > pos.quantity:
                if held < 0:
                    await self._cancel_protective(pos)
                pos.execution_state = "attention"
                pos.execution_error = f"Broker net qty {held} differs from tracked {pos.quantity}; manual review required."
                self._log(pos.execution_error, level="error")
                self._persist()
            elif pos.exit_order_id or pos.exit_correlation_id:
                try:
                    order = await self._order_state(pos.exit_order_id, pos.exit_correlation_id)
                    pos.exit_order_id = str(order.get("orderId") or pos.exit_order_id or "") or None
                    status = str(order["orderStatus"]).upper()
                    if status in TERMINAL_ORDERS:
                        # A TRADED exit can precede the position book update.
                        if status != "TRADED":
                            pos.execution_error = f"Exit {status}: {order.get('omsErrorDescription') or 'retry after broker sync'}"
                            pos.exit_order_id = pos.exit_correlation_id = None
                            pos.execution_state = "open"
                            if held != pos.quantity:
                                pos.entry_quantity = pos.entry_quantity or pos.quantity
                                pos.partial_exit = True
                            pos.quantity = held
                            pos.next_exit_try_at = time.time() + 3.0
                    self._persist()
                except DhanAPIError as exc:
                    self._broker_sync_error = exc.error_message
            elif held != pos.quantity:
                if not await self._cancel_protective(pos):
                    return result
                if pos.exit_order_id or time.time() < pos.next_exit_try_at:
                    return result
                if not pos.entry_quantity:
                    pos.entry_quantity = pos.quantity
                pos.partial_exit = True
                self._log(
                    f"Reconcile: holding {held} but tracking {pos.quantity}; adopting Dhan qty",
                    level="warn")
                pos.quantity = held
                if self.cfg.protective_sl:
                    await self._place_protective_sl(pos)
                self._persist()
            elif self.cfg.protective_sl and not pos.protective_order_id:
                await self._place_protective_sl(pos)
            elif pos.protective_order_id:
                try:
                    order = await self._order_state(pos.protective_order_id, pos.protective_correlation_id)
                    status = str(order["orderStatus"]).upper()
                    if status in {"REJECTED", "CANCELLED", "EXPIRED"}:
                        pos.execution_state = "attention"
                        pos.execution_error = f"Protective SL {status}; broker protection is not active. Check Dhan."
                        self._log(pos.execution_error, level="error")
                        self._persist()
                except DhanAPIError as exc:
                    self._broker_sync_error = exc.error_message
        else:
            self._broker_net_qty = None
            for sid, qty in net.items():
                if qty > 0:
                    self._log(
                        f"Reconcile: untracked position {sid} x{qty} found at Dhan "
                        f"- NOT managed by algo. Please square off manually if unwanted.",
                        level="error")
        result["tracked"] = bool(self._position)
        return result

    # ---------------------------------------------------------------- toggles
    async def _preflight_ip(self) -> Dict[str, Any]:
        """Refuse to arm LIVE when Dhan would reject our outbound IP.

        Dhan whitelists order APIs against a static IP, so a mismatch means
        DH-905 on every entry. Catching it at arm time beats discovering it at
        the 09:25 breakout, which is exactly what happened on 2026-09-28.
        """
        s = get_settings()
        if not s.is_configured():
            return {"ok": False, "reason": "Dhan credentials are not configured"}
        try:
            info = await get_dhan_client().get_ip() or {}
        except DhanAPIError as exc:
            return {"ok": False, "reason": f"could not read static IP: {exc.error_message}"}
        status = str(info.get("ipMatchStatus") or "").upper()
        # NOTE: never substring-match "MATCH" - "MISMATCH" contains it.
        allowed = info.get("ordersAllowed")
        if allowed is not None:
            ok = bool(allowed)
        else:
            ok = status in ("PRIMARY_MATCH", "SECONDARY_MATCH")
        if ok:
            return {"ok": True, "reason": f"Dhan accepts our IP ({status or 'ordersAllowed'})"}
        detected = info.get("detectedIP")
        if not detected:
            detected = await get_dhan_client().detect_public_ip()
        return {
            "ok": False,
            "reason": (
                f"Dhan would reject our orders: registered="
                f"{info.get('primaryIP') or '-'} / {info.get('secondaryIP') or '-'} "
                f"but we egress as {detected} ({status or 'MISMATCH'}). "
                f"Register {detected} via Settings > Static IP."
            ),
            "primary": info.get("primaryIP"),
            "secondary": info.get("secondaryIP"),
            "detected": detected,
            "ipMatchStatus": status,
            "ordersAllowed": info.get("ordersAllowed"),
            "modifyDatePrimary": info.get("modifyDatePrimary"),
            "modifyDateSecondary": info.get("modifyDateSecondary"),
        }

    async def arm(self) -> Dict[str, Any]:
        # LIVE preflight: never arm into a state where every order will bounce.
        if not self._is_dry():
            check = await self._preflight_ip()
            if not check.get("ok"):
                self._log(f"ARM REFUSED (live): {check['reason']}", level="error")
                return {**self.status(), "armed": False, "armError": check}
            self._log(f"IP preflight OK: {check['reason']}", level="info")
        self.enabled = True
        self._log(f"ALGO ARMED ({'DRY-RUN' if self._is_dry() else 'LIVE'})", level="success",
                  notification="algo_armed")
        await self.reconcile()
        # Run one supervisor pass immediately so the returned status already
        # shows the real phase (otherwise the UI briefly says "OFF").
        await self._tick_supervisor()
        self._persist()
        return self.status()

    def disarm(self, reason: str = "operator") -> Dict[str, Any]:
        self.enabled = False
        self._log(f"ALGO DISARMED ({reason})"
                  + (" - new entries stopped; open-position management continues" if self._position else ""),
                  level="warn", notification="algo_disarmed")
        self._persist()
        return self.status()

    async def panic_exit(self, reason: str = "PANIC button") -> Dict[str, Any]:
        self._log(f"PANIC EXIT requested: {reason}", level="error")
        if self._position is None:
            self._log("Panic exit: no open position", level="warn")
            return self.status()
        await self._exit_position(self._position, "PANIC")
        return self.status()

    def set_exit_rules(self, profit_target_rs: float, stop_loss_rs: float) -> None:
        """Rupee exits only tighten/loosen exits, so they may change while armed or in a position."""
        for value in (profit_target_rs, stop_loss_rs):
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise HTTPException(400, "Profit/loss conditions must be finite and >= 0 (0 = off)")
        self.cfg.profit_target_rs = float(profit_target_rs)
        self.cfg.stop_loss_rs = float(stop_loss_rs)
        self._log(f"Exit conditions saved: profit >= Rs{profit_target_rs:,.0f}"
                  f"{'' if profit_target_rs else ' (off)'}, loss <= -Rs{stop_loss_rs:,.0f}"
                  f"{'' if stop_loss_rs else ' (off)'}")
        self._persist()

    def set_config(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        if self._position is not None or self._execution_lock.locked():
            raise HTTPException(409, "Cannot change algo configuration while execution/position is active.")
        allowed = set(asdict(self.cfg).keys())
        cfg = type(self.cfg)(**{**asdict(self.cfg), **{
            k: v for k, v in (patch or {}).items() if k in allowed and v is not None
        }})
        for key in ("sl_mult", "target_mult", "risk_pct", "max_capital_pct", "protective_sl_mult"):
            value = getattr(cfg, key)
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise HTTPException(400, f"{key} must be a finite positive number")
        if (cfg.risk_pct > 100 or cfg.max_capital_pct > 100 or cfg.protective_sl_mult < 1
                or cfg.min_lots < 1 or cfg.max_lots < cfg.min_lots):
            raise HTTPException(400, "Invalid risk/capital percentage, lot bounds or protective multiplier")
        for key in ("breakeven_at", "min_or_pct", "max_or_pct", "profit_target_rs", "stop_loss_rs"):
            value = getattr(cfg, key)
            if not math.isfinite(value) or value < 0:
                raise HTTPException(400, f"{key} must be finite and nonnegative")
        if cfg.max_or_pct and cfg.min_or_pct > cfg.max_or_pct:
            raise HTTPException(400, "min_or_pct cannot exceed max_or_pct")
        if not isinstance(cfg.max_trades, int) or not 1 <= cfg.max_trades <= 5:
            raise HTTPException(400, "max_trades must be between 1 and 5")
        if cfg.order_type not in {"MARKET", "LIMIT"} or cfg.product_type not in {"INTRADAY", "MARGIN"}:
            raise HTTPException(400, "Unsupported algo order/product type")
        if not cfg.indices or any(get_index(key) is None for key in cfg.indices):
            raise HTTPException(400, "Select at least one supported index")
        entry_min = _to_min(cfg.entry_cutoff, -1)
        exit_min = _to_min(cfg.exit_time, -1)
        for value in (cfg.entry_cutoff, cfg.exit_time):
            try:
                datetime.strptime(value, "%H:%M")
            except ValueError:
                raise HTTPException(400, "Use valid HH:MM times in IST")
        if not OR_READY_MIN <= entry_min <= exit_min <= MARKET_CLOSE_MIN:
            raise HTTPException(400, "Require 09:25 <= entry cutoff <= exit time <= 15:30 IST")
        self.cfg = cfg
        self._log("Configuration updated by operator")
        self._subscribe_tracked()
        self._persist()
        return self.status()

    # ------------------------------------------------------------ supervisor
    async def _supervisor(self) -> None:
        while self._running:
            try:
                await self._tick_supervisor()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._last_error = str(exc)
                logger.exception("account=%s algo supervisor error", self.account_id or current_account())
            await asyncio.sleep(0.5)

    async def _tick_supervisor(self) -> None:
        async with self._execution_lock:
            await self._tick_supervisor_locked()

    async def _tick_supervisor_locked(self) -> None:
        now = now_ist()
        today = now.date().isoformat()
        minute = now.hour * 60 + now.minute
        ft = feed.status()
        self._feed_connected = bool(ft.get("webSocket"))

        if self._position is not None and time.monotonic() - self._last_reconcile_at >= RECONCILE_SECONDS:
            await self._reconcile_locked()
        if self._day != today and self._position is None:
            self._reset_day(today, now)

        if self._position is not None:
            self.phase = self._position.execution_state if self._position.execution_state != "open" else "in_trade"
            if (self._position.execution_state == "entry_pending"
                    and (self._day != today or minute >= self._exit_min(now))):
                await self._exit_position_locked(self._position, "TIME")
                return
            if self._position.execution_state in {"open", "attention"}:
                if self._position.exit_reason:
                    await self._exit_position_locked(self._position, self._position.exit_reason)
                elif self._day != today or minute >= self._exit_min(now):
                    await self._exit_position_locked(self._position, "TIME")
                else:
                    await self._manage_position()
            return
        if not self.enabled:
            self.phase = "off"
            return

        sess = session_for(now)
        if not sess.trading_day:
            self.phase = "closed"
            return
        if minute < sess.open_min:
            self.phase = "preopen"
            return
        if minute >= sess.close_min:
            self.phase = "closed"
            return

        # OR build + watch
        if minute >= sess.or_ready_min:
            if self.account_id == "child":
                master = self._group.items["master"]
                if self._position is None and (
                        master._signal_day != today
                        or any(l.status == "watching" for l in master._legs.values())):
                    self._legs = {key: DayLeg(**asdict(leg)) for key, leg in master._legs.items()}
            else:
                await self._maybe_compute_or()
                self._maybe_open_next_trade(today, minute)

        # Forced square-off
        if self._position is not None and minute >= self._exit_min(now):
            await self._exit_position_locked(self._position, "TIME")

        if self._position is not None:
            self.phase = "in_trade"
        elif any(l.status == "watching" for l in self._legs.values()):
            self.phase = "watching"
        elif minute < sess.or_ready_min:
            self.phase = "building_or"
        else:
            self.phase = "day_done"

    def _reset_day(self, today: str, now: datetime) -> None:
        if self._day is not None:
            self._log(f"New session {today} - previous day archived", level="info",
                      previousDay=self._day)
        self._day = today
        self._position = None
        self._today_trades = []
        self._recovered = None
        self._legs = {k: DayLeg(k) for k in self.cfg.indices}
        self.phase = "preopen" if now.hour * 60 + now.minute < session_for(now).open_min else "building_or"
        self._persist()

    def _maybe_open_next_trade(self, today: str, minute: int) -> None:
        """Once every account is flat after a consumed signal, re-watch both indices."""
        used = self._signals_used(today)
        if (self.account_id != "master" or not 0 < used < self._max_trades()
                or minute >= self._cutoff_min()
                or any(e._position is not None for e in self._group.items.values())
                or any(l.status == "watching" for l in self._legs.values())):
            return
        reopened = []
        for key, leg in self._legs.items():
            if leg.or_high is None or leg.or_low is None:
                continue
            leg.status = "watching"
            leg.rearm = True
            leg.blocked_side = ""
            leg.signal = None
            leg.entry_attempts = 0
            leg.next_try_at = 0.0
            leg.error = ""
            leg.lot_cap = None
            leg.funds_retries = 0
            leg.note = f"Trade {used + 1}: waiting for a fresh breakout"
            reopened.append(key)
        if reopened:
            self._log(f"Trade {used}/{self._max_trades()} done and all accounts flat; watching "
                      f"{', '.join(reopened)} for a fresh breakout (trade {used + 1}). "
                      f"A side spot is already beyond needs spot back inside the range first.",
                      level="info")
            self._persist()

    # -------------------------------------------------------------- OR build
    def _can_compute_or(self) -> bool:
        now = now_ist()
        sess = session_for(now)
        return sess.trading_day and (now.hour * 60 + now.minute) >= sess.or_ready_min

    async def _intraday(self, **kwargs: Any) -> Dict[str, Any]:
        """Intraday fetch with throttle + 429 backoff (arming fires 2 back-to-back)."""
        client = get_data_client() if self.account_id is not None else get_dhan_client()
        last: Optional[DhanAPIError] = None
        for attempt, delay in enumerate((0.0, 1.5, 3.0, 6.0)):
            if delay:
                await asyncio.sleep(delay)
            try:
                return await client.get_intraday(**kwargs)
            except DhanAPIError as exc:
                last = exc
                if exc.status_code != 429 and "904" not in str(exc.error_code):
                    raise
                logger.info("intraday 429; retry %d in %.1fs", attempt + 1, delay)
        raise last if last else DhanAPIError(429, "DH-904", "rate limited")

    def _record_tick_or(self, security_id: int, ltp: float, now: Optional[datetime] = None) -> None:
        now = now or now_ist()
        sess = session_for(now)
        minute = now.hour * 60 + now.minute
        if not sess.trading_day or not sess.open_min <= minute <= sess.or_end_min or ltp <= 0:
            return
        for key in self.cfg.indices:
            inst = get_index(key)
            if inst is None or inst.sec_id != security_id:
                continue
            day = now.date().isoformat()
            rec = self._tick_or.get(key)
            if rec is None or rec["day"] != day:
                rec = self._tick_or[key] = {"day": day, "high": ltp, "low": ltp, "minutes": set()}
            rec["high"] = max(rec["high"], ltp)
            rec["low"] = min(rec["low"], ltp)
            rec["minutes"].add(minute)
            return

    def _tick_or_range(self, key: str, day: str, sess: Session) -> Optional[tuple]:
        """(high, low) from live ticks, only when every OR minute saw at least one tick."""
        rec = self._tick_or.get(key)
        if rec is None or rec["day"] != day:
            return None
        if not set(range(sess.open_min, sess.or_end_min + 1)) <= rec["minutes"]:
            return None
        return rec["high"], rec["low"]

    async def _maybe_compute_or(self, force: bool = False) -> None:
        if not self._can_compute_or():
            return
        pending = [
            k for k in self.cfg.indices
            if self._legs.get(k) is None or self._legs[k].or_high is None
        ]
        if not pending:
            return
        today = now_ist().date()
        sess = session_for(now_ist())
        or_start, or_end = sess.open_min, sess.or_end_min
        for key in pending:
            leg = self._legs.setdefault(key, DayLeg(key))
            inst = get_index(key)
            if inst is None:
                leg.status = "skipped"
                leg.note = "unknown index"
                continue
            try:
                raw = await self._intraday(
                    security_id=str(inst.sec_id),
                    exchange_segment="IDX_I",
                    instrument="INDEX",
                    interval="1",
                    from_dt=(f"{today.isoformat()} {INTRADAY_FROM_TIME}" if not sess.special
                             else f"{today.isoformat()} {_hhmm(or_start - 1)}:00"),
                    to_dt=f"{today.isoformat()} {_hhmm(sess.or_ready_min + 1)}:00",  # later candles filtered below
                )
            except DhanAPIError as exc:
                self._last_error = exc.error_message
                now_min = now_ist().hour * 60 + now_ist().minute
                backup = self._tick_or_range(key, today.isoformat(), sess)
                if backup is None or now_min < sess.or_ready_min + 1:
                    leg.note = f"OR fetch failed: {exc.error_message}"
                    continue
                leg.or_high, leg.or_low = backup
                leg.status = "watching"
                leg.note = ""
                self._log(
                    f"{key} opening range locked from LIVE TICKS (Dhan candle API failed: "
                    f"{exc.error_message})  H {leg.or_high:,.1f}  L {leg.or_low:,.1f}  "
                    f"({leg.or_range:,.1f} pts)",
                    level="warn", index=key, orHigh=leg.or_high, orLow=leg.or_low,
                )
                feed.subscribe(inst.sec_id, "IDX_I")
                asyncio.ensure_future(self._prepare_contract(leg))
                continue
            highs, lows, ts, minutes = [], [], [], set()
            data = raw.get("data") if isinstance(raw, dict) and "data" in raw else raw
            if isinstance(data, dict):
                ts = data.get("timestamp") or []
                h = data.get("high") or []
                lo = data.get("low") or []
                for i in range(min(len(ts), len(h), len(lo))):
                    try:
                        t = datetime.fromtimestamp(int(ts[i]), IST)
                        mv = float(h[i])
                        lv = float(lo[i])
                    except (TypeError, ValueError, OSError, OverflowError):
                        continue
                    m = t.hour * 60 + t.minute
                    if or_start <= m <= or_end and mv > 0 and lv > 0:
                        highs.append(mv)
                        lows.append(lv)
                        minutes.add(m)
            if not highs or not lows:
                leg.note = "no opening-range candles yet"
                continue
            if or_start not in minutes:
                # The 09:15 candle usually holds the day's opening extreme; never lock without it.
                note = f"{_hhmm(or_start)} candle missing from Dhan data - retrying OR fetch"
                if leg.note != note:
                    self._log(f"{key} {note}", level="warn", index=key)
                leg.note = note
                continue
            if or_end not in minutes:
                # Right at 09:25 Dhan may not have published the 09:24 candle yet.
                leg.note = f"waiting for the {_hhmm(or_end)} candle"
                continue
            leg.or_high = max(highs)
            leg.or_low = min(lows)
            leg.status = "watching"
            leg.note = ""
            self._log(
                f"{key} opening range locked  H {leg.or_high:,.1f}  L {leg.or_low:,.1f}  "
                f"({leg.or_range:,.1f} pts)",
                level="success", index=key, orHigh=leg.or_high, orLow=leg.or_low,
            )
            feed.subscribe(inst.sec_id, "IDX_I")
            asyncio.ensure_future(self._prepare_contract(leg))
        self._persist()

    async def _prepare_contract(self, leg: DayLeg, direction: Optional[str] = None) -> None:
        """Resolve the option contract for `direction`.

        Before the breakout the side is unknown, so we warm BOTH and only
        subscribe the feed. At entry the caller MUST pass the real direction:
        the previous version looped ("CALL", "PUT") and broke on the first hit,
        so security_id was ALWAYS the CE - a bearish breakout bought a CALL and
        then managed it with PUT levels.
        """
        if leg.or_high is None:
            return
        inst = get_index(leg.index_key)
        snap = chain_engine.snapshot(leg.index_key)
        if not snap or not snap.get("rows"):
            warm = getattr(chain_engine, "warm", None)
            if warm:
                try:
                    await warm(leg.index_key)
                except Exception:  # noqa: BLE001
                    pass
                snap = chain_engine.snapshot(leg.index_key)
        if not snap or not snap.get("rows"):
            leg.note = "option chain not ready"
            return
        rows = snap["rows"]
        spot = leg.spot or float(snap.get("underlyingLtp") or 0)
        step = self._strike_step(rows, inst.strike_step if inst else 50)
        if not spot or step <= 0:
            leg.note = "no spot / strike step"
            return
        leg.expiry = snap.get("expiry")
        if leg.spot is None:
            leg.spot = spot

        sides = (direction,) if direction in ("CALL", "PUT") else ("CALL", "PUT")
        for side in sides:
            # Decide the strike from the breakout level itself, so the contract warmed at
            # OR lock is exactly the one traded and its premium is already streaming.
            # Fall back to live spot only if price is already far past the level.
            level = leg.or_high if side == "CALL" else leg.or_low
            ref = level if level and (direction is None or abs(spot - level) <= 2 * step) else spot
            strike = self._pick_strike(ref, step, side, self.cfg.strike_offset)
            row = next((r for r in rows if abs(float(r["strike"]) - strike) < 1e-6), None)
            if row is None:
                continue
            node = row["ce" if side == "CALL" else "pe"] or {}
            sec = _as_int(node.get("securityId"))
            if not sec:
                continue
            # Always subscribe, so the tick feed is warm either way.
            feed.subscribe(sec, inst.derivative_segment if inst else "NSE_FNO")
            if direction is None:
                continue          # pre-breakout: warming only, do not commit
            leg.strike = strike
            leg.security_id = sec
            leg.option_ltp = _as_float(node.get("ltp"))
            leg.prepared_side = side
            self._log(
                f"{leg.index_key} contract ready: {int(strike)} "
                f"{'CE' if side == 'CALL' else 'PE'} "
                f"(ATM{'+' if self.cfg.strike_offset > 0 else ''}{self.cfg.strike_offset}) "
                f"premium ~{leg.option_ltp}",
                level="info", index=leg.index_key, side=side, strike=strike,
            )
            return

    @staticmethod
    def _strike_step(rows: List[Dict[str, Any]], fallback: int) -> int:
        strikes = sorted({float(r["strike"]) for r in rows if r.get("strike")})
        gaps = [round(strikes[i + 1] - strikes[i]) for i in range(len(strikes) - 1)]
        gaps = [g for g in gaps if g > 0]
        if not gaps:
            return fallback
        return min(gaps)

    @staticmethod
    def _pick_strike(spot: float, step: int, direction: str, offset: int) -> float:
        atm = round(spot / step) * step
        return atm + offset * step if direction == "CALL" else atm - offset * step

    # ------------------------------------------------------------- tick loop
    def _on_tick(self, security_id: int, ltp: float, segment: Optional[str] = None) -> None:
        """Called from the feed on every price change. Must stay non-blocking."""
        if segment in (None, "IDX_I") and self.account_id != "child":
            self._record_tick_or(security_id, ltp)
        for leg in self._legs.values():
            if leg.index_key in self.cfg.indices and get_index(leg.index_key) and \
                    get_index(leg.index_key).sec_id == security_id and segment in (None, "IDX_I"):
                leg.spot = round(ltp, 2)
                break
            if leg.security_id == security_id and segment in (None, self._segment_for(leg.index_key)):
                leg.option_ltp = ltp
        if self._position is not None:
            inst = get_index(self._position.index_key)
            if inst and inst.sec_id == security_id and segment in (None, "IDX_I"):
                self._position.spot = round(ltp, 2)
            elif (self._position.security_id == security_id
                  and segment in (None, self._segment_for(self._position.index_key))):
                self._position.premium = ltp
        if self._running and (self.enabled or self._position is not None) and not self._busy:
            self._busy = True
            self._evaluation_task = asyncio.ensure_future(self._evaluate())

    async def _evaluate(self) -> None:
        """Decide whether to enter or exit, right now."""
        try:
            async with self._execution_lock:
                if self._position is not None:
                    await self._manage_position()
                elif self.enabled:
                    await self._look_for_entry()
        except Exception as exc:
            self._last_error = str(exc)
            logger.exception("Algo tick evaluation failed")
        finally:
            self._busy = False

    def _rupee_pnl(self, pos: Position) -> Optional[float]:
        """Live premium P&L of the held quantity, or None when the premium is not fresh."""
        if pos.premium is None or not pos.entry_premium or pos.quantity <= 0:
            return None
        age = feed.age(pos.security_id, self._segment_for(pos.index_key))
        if age is None or age > MAX_PRICE_AGE:
            return None
        return (pos.premium - pos.entry_premium) * pos.quantity

    def _rupee_exit_reason(self, pos: Position) -> Optional[str]:
        target, stop = self.cfg.profit_target_rs, self.cfg.stop_loss_rs
        if not target and not stop:
            return None
        pnl = self._rupee_pnl(pos)
        if pnl is None:
            return None
        # Half-paisa tolerance so float noise (e.g. 1199.9999999) never skips an exact hit.
        if target and pnl >= target - 0.005:
            return "PROFIT_RS"
        if stop and pnl <= -stop + 0.005:
            return "LOSS_RS"
        return None

    async def _maybe_rupee_exit_locked(self) -> Optional[str]:
        """Exit now if the held position's fresh premium P&L meets a rupee rule. Caller holds the lock."""
        pos = self._position
        if (pos is None or pos.execution_state not in {"open", "attention"}
                or pos.exit_order_id or pos.exit_correlation_id or time.time() < pos.next_exit_try_at):
            return None
        reason = self._rupee_exit_reason(pos)
        if reason:
            pnl = self._rupee_pnl(pos) or 0.0
            self._log(f"{pos.index_key}: premium P&L Rs{pnl:,.0f} hit "
                      f"{'profit' if reason == 'PROFIT_RS' else 'loss'} condition -> exit",
                      level="warn", index=pos.index_key)
            await self._exit_position_locked(pos, reason)
        return reason

    async def check_rupee_exit_now(self) -> Optional[str]:
        """Apply freshly saved rupee rules at once instead of waiting for the next tick."""
        if self._position is None:
            return None
        async with self._execution_lock:
            return await self._maybe_rupee_exit_locked()

    async def _manage_position(self) -> None:
        pos = self._position
        if await self._maybe_rupee_exit_locked():
            return
        if pos is None or pos.spot is None or pos.execution_state not in {"open", "attention"}:
            return
        if self.cfg.target_only and pos.dry_run and self.cfg.protective_sl:
            premium_age = feed.age(pos.security_id, self._segment_for(pos.index_key))
            if (pos.premium is not None and premium_age is not None and premium_age <= MAX_PRICE_AGE
                    and pos.premium <= self._protective_trigger(pos)):
                await self._exit_position_locked(pos, "PROTECTIVE_SL")
                return
        inst = get_index(pos.index_key)
        age = feed.age(inst.sec_id, "IDX_I") if inst else None
        if age is None or age > MAX_PRICE_AGE:
            return
        d = pos.spot - pos.entry_spot
        moved = d if pos.option_type == "CALL" else -d
        pos.mfe = max(pos.mfe, moved)
        pos.mae = min(pos.mae, moved)

        # Break-even ratchet
        if (not self.cfg.target_only and self.cfg.breakeven_at > 0 and not pos.breakeven_done
                and moved >= self.cfg.breakeven_at * pos.or_range):
            pos.breakeven_done = True
            pos.stop_spot = pos.entry_spot
            self._log(f"{pos.index_key}: +{moved:,.1f} pts reached -> stop moved to break-even",
                      level="success", index=pos.index_key)
            self._persist()

        hit_stop = (pos.spot <= pos.stop_spot) if pos.option_type == "CALL" else (pos.spot >= pos.stop_spot)
        hit_target = (pos.spot >= pos.target_spot) if pos.option_type == "CALL" else (pos.spot <= pos.target_spot)
        if hit_stop and not self.cfg.target_only:
            await self._exit_position_locked(pos, "BREAKEVEN" if pos.breakeven_done and
                                      abs(pos.stop_spot - pos.entry_spot) < 1e-6 else "STOP")
        elif hit_target:
            await self._exit_position_locked(pos, "TARGET")

    async def _look_for_entry(self) -> None:
        if self.account_id == "child":
            return
        now = now_ist()
        minute = now.hour * 60 + now.minute
        today = now.date().isoformat()
        sess = session_for(now)
        if (not sess.trading_day or minute < sess.or_ready_min
                or minute >= self._cutoff_min(now)
                or len(self._today_trades) >= self._max_trades(now)
                or self._signals_used(today) >= self._max_trades(now)):
            return
        if self.account_id is not None and any(e._position is not None for e in self._group.items.values()):
            return
        for key in self.cfg.indices:
            leg = self._legs.get(key)
            if leg is None or leg.status != "watching" or leg.or_high is None or leg.spot is None:
                continue
            if time.time() < leg.next_try_at:
                continue   # backing off after a rejected entry
            inst = get_index(key)
            age = feed.age(inst.sec_id, "IDX_I") if inst else None
            if age is None or age > MAX_PRICE_AGE:
                leg.note = "Waiting for a fresh spot quote (maximum age 5s)"
                continue
            side = ("CALL" if leg.spot > leg.or_high
                    else "PUT" if leg.or_low is not None and leg.spot < leg.or_low else "")
            if leg.rearm:
                leg.rearm = False
                leg.blocked_side = side
                if side:
                    leg.note = (f"Spot {leg.spot} already beyond OR {'high' if side == 'CALL' else 'low'}; "
                                "waiting for it to return inside the range")
                continue
            if side and side == leg.blocked_side:
                continue
            if not side and leg.blocked_side:
                leg.blocked_side = ""
                leg.note = "Back inside the range; the next breakout will trade"
                self._log(f"{key} back inside the range ({leg.spot}); fresh breakout armed", index=key)
                continue
            rng = leg.or_range or 0.0
            if rng <= 0:
                continue
            or_pct = rng / leg.spot * 100.0
            if self.cfg.min_or_pct and or_pct < self.cfg.min_or_pct:
                leg.status = "skipped"
                leg.note = f"OR {or_pct:.2f}% below min {self.cfg.min_or_pct}%"
                self._log(f"{key} skipped: {leg.note}", level="warn")
                continue
            if self.cfg.max_or_pct and or_pct > self.cfg.max_or_pct:
                leg.status = "skipped"
                leg.note = f"OR {or_pct:.2f}% above max {self.cfg.max_or_pct}%"
                self._log(f"{key} skipped: {leg.note}", level="warn")
                continue
            if leg.spot > leg.or_high:
                if self.account_id is not None:
                    await self._group.enter_signal(leg, "CALL")
                else:
                    await self._enter(leg, "CALL")
                return
            if leg.spot < leg.or_low:
                if self.account_id is not None:
                    await self._group.enter_signal(leg, "PUT")
                else:
                    await self._enter(leg, "PUT")
                return

    # ---------------------------------------------------------------- entry
    async def _enter(self, leg: DayLeg, direction: str) -> None:
        rng = leg.or_range or 0.0
        spot = leg.spot or 0.0
        if rng <= 0 or spot <= 0:
            return
        if self.account_id is None or leg.prepared_side != direction or not leg.security_id:
            await self._prepare_contract(leg, direction)
        if not leg.security_id or leg.strike is None:
            leg.note = "contract not ready - cannot enter"
            self._log(f"{leg.index_key}: breakout seen but {direction} contract not ready",
                      level="error")
            return
        if leg.prepared_side != direction:
            # Fail-safe: never send an order whose option side disagrees with the
            # breakout. Silence here is how a PUT signal buys a CALL.
            leg.note = f"contract side mismatch ({leg.prepared_side} != {direction})"
            self._log(
                f"{leg.index_key}: ABORT entry - prepared {leg.prepared_side or '?'} "
                f"contract but breakout is {direction}",
                level="error", index=leg.index_key)
            leg.status = "error"
            self._persist()
            return
        premium = feed.price(leg.security_id, self._segment_for(leg.index_key))
        premium_age = feed.age(leg.security_id, self._segment_for(leg.index_key))
        if premium is None or premium_age is None or premium_age > MAX_PRICE_AGE:
            leg.note = "Waiting for fresh option premium before entry (maximum age 5s)"
            return
        leg.option_ltp = premium

        balance: Optional[float] = None
        if not self._is_dry():
            try:
                client = get_dhan_client()
                # One parallel round-trip instead of four sequential REST calls.
                held, book, supers, balance = await asyncio.gather(
                    client.get_positions(fresh=True), client.get_order_book(fresh=True),
                    client.get_super_orders(fresh=True), self._balance())
            except DhanAPIError as exc:
                leg.note = f"Entry blocked: cannot verify broker holdings/balance: {exc.error_message}"
                self._log(leg.note, level="error")
                return
            def matches(row: Dict[str, Any]) -> bool:
                return (str(row.get("securityId")) == str(leg.security_id)
                        and row.get("exchangeSegment") == self._segment_for(leg.index_key)
                        and row.get("productType") == self.cfg.product_type)
            if any(matches(r) and int(r.get("netQty") or 0) != 0 for r in held):
                leg.status = "skipped"
                leg.note = "Contract already held at broker; refusing to mix manual/algo quantity."
                self._log(leg.note, level="error")
                self._persist()
                return
            if any(matches(row) and str(row.get("orderStatus") or "").upper() not in TERMINAL_ORDERS
                   for row in [*book, *supers]):
                leg.note = "Entry blocked: another order for this contract is pending at Dhan."
                self._log(leg.note, level="warn")
                return

        # Set the signal BEFORE sizing so _delta_for resolves the right leg.
        leg.signal = direction
        sizing = await self._size_position(leg, balance=balance)
        if leg.lot_cap is not None and sizing["lots"] > leg.lot_cap:
            per_lot = sizing["quantity"] // sizing["lots"]
            sizing = {**sizing, "lots": leg.lot_cap, "quantity": leg.lot_cap * per_lot,
                      "reason": f"{sizing.get('reason', '')} -> {leg.lot_cap} lot(s) after funds rejection"}
        if sizing["lots"] < max(1, self.cfg.min_lots):
            leg.status = "skipped"
            leg.note = sizing["reason"]
            self._log(f"{leg.index_key}: breakout skipped - {sizing['reason']}", level="warn")
            self._persist()
            return

        inst = get_index(leg.index_key)
        spot_age = feed.age(inst.sec_id, "IDX_I") if inst else None
        live_spot = leg.spot or 0.0
        # A funds retry re-sends an already accepted signal immediately; no breakout recheck.
        if not leg.funds_retries and (spot_age is None or spot_age > MAX_PRICE_AGE or (
            direction == "CALL" and live_spot <= (leg.or_high or 0)
        ) or (direction == "PUT" and live_spot >= (leg.or_low or 0))):
            leg.note = "Breakout no longer valid on a fresh spot quote; waiting."
            return
        if self.account_id is None:
            spot = live_spot
        target, stop = self._levels(leg, direction, spot, rng, sizing)

        pos = self.POSITION_CLS(
            index_key=leg.index_key, option_type=direction,
            security_id=leg.security_id, strike=float(leg.strike),
            expiry=leg.expiry or "", lots=sizing["lots"], quantity=sizing["quantity"],
            entry_spot=spot, entry_premium=leg.option_ltp or 0.0,
            entry_time=now_ist().strftime("%H:%M:%S"), entry_ts=time.time(),
            or_range=rng, target_spot=round(target, 2), stop_spot=round(stop, 2),
            initial_stop_spot=round(stop, 2), dry_run=self._is_dry(),
            spot=spot, premium=leg.option_ltp,
        )
        t0 = getattr(self, "_signal_t0", None)
        self._log(
            f"BREAKOUT {leg.index_key} {'UP' if direction == 'CALL' else 'DOWN'} @ {spot:,.1f} "
            f"-> BUY {direction} {int(leg.strike)} x{pos.quantity} ({pos.lots} lot) "
            f"@~{leg.option_ltp} | {sizing.get('reason', '')}"
            f"{f' (+{(time.perf_counter() - t0) * 1000:.0f}ms since signal)' if t0 else ''}"
            f"{' [DRY-RUN]' if pos.dry_run else ''}",
            level="success", index=leg.index_key, direction=direction,
            strike=pos.strike, lots=pos.lots, spot=spot, target=pos.target_spot, stop=pos.stop_spot,
        )
        leg.status = "active"
        leg.signal = direction
        leg.planned_lots = sizing["lots"]

        # Place the entry
        pos.execution_state = "entry_pending"
        pos.entry_correlation_id = f"{self.ORDER_TAG}-E-{uuid.uuid4().hex[:18]}"
        self._position = pos
        self._persist()
        fill = await self._send_order(
            side="BUY", security_id=pos.security_id, qty=pos.quantity,
            segment=self._segment_for(leg.index_key), order_type=self.cfg.order_type,
            price=pos.entry_premium, correlation_id=pos.entry_correlation_id,
            tag="entry", dry_run=pos.dry_run,
        )
        if not fill.get("ok"):
            if fill.get("uncertain"):
                pos.execution_error = "Entry outcome unknown; checking correlation ID. No repeat BUY will be sent."
                self._last_error = pos.execution_error
                self._persist()
                return
            self._position = None
            code = str(fill.get("code") or "")
            msg = str(fill.get("error") or "order failed")
            if await self._retry_fewer_lots(leg, direction, pos.lots, f"{code} {msg}"):
                return
            leg.entry_attempts += 1
            leg.signal = None
            leg.error = f"{code}: {msg}" if code else msg
            self._last_error = msg

            if code in FATAL_ORDER_CODES:
                # A config problem (e.g. static IP not whitelisted). Every future
                # order fails the same way, so do NOT retry - stop and disarm.
                leg.status = "error"
                leg.note = f"entry rejected ({code}): {msg}"
                self._log(
                    f"{leg.index_key}: FATAL order error {code} - {msg}. "
                    f"Orders cannot be placed; DISARMING without retry. "
                    f"Fix the cause (e.g. register the static IP in your Dhan "
                    f"profile) and re-arm.",
                    level="error", index=leg.index_key, code=code,
                )
                self.disarm(f"fatal order error {code}")
                self._persist()
                return

            if leg.entry_attempts >= ENTRY_MAX_ATTEMPTS:
                leg.status = "error"
                leg.note = f"entry failed {leg.entry_attempts}x: {msg}"
                self._log(
                    f"{leg.index_key}: entry failed {leg.entry_attempts} times "
                    f"({msg}) - giving up for today",
                    level="error", index=leg.index_key,
                )
            else:
                i = min(leg.entry_attempts - 1, len(ENTRY_RETRY_BACKOFF) - 1)
                delay = ENTRY_RETRY_BACKOFF[i]
                leg.next_try_at = time.time() + delay
                leg.status = "watching"
                self._log(
                    f"{leg.index_key}: entry attempt {leg.entry_attempts}/"
                    f"{ENTRY_MAX_ATTEMPTS} failed ({msg}) - retrying in {delay:.0f}s",
                    level="warn", index=leg.index_key,
                )
            self._persist()
            return

        pos.order_id = fill.get("orderId")
        if pos.dry_run:
            pos.execution_state = "open"
            pos.entry_confirmed_ts = time.time()
        else:
            try:
                await self._refresh_entry(pos)
            except DhanAPIError as exc:
                pos.execution_error = f"Entry awaiting broker confirmation: {exc.error_message}"
                self._log(pos.execution_error, level="warn")
            if self._position is None and leg.status == "error":
                await self._retry_fewer_lots(leg, direction, pos.lots, leg.note)
                return
        self.phase = "in_trade" if pos.execution_state == "open" else pos.execution_state

        # Lock out the other index for this trade.
        for other in self.cfg.indices:
            if other != leg.index_key and self._legs.get(other, DayLeg(other)).status == "watching":
                o = self._legs[other]
                o.status = "locked"
                o.note = f"{leg.index_key} took today's trade"

        if pos.dry_run and self.cfg.protective_sl:
            await self._place_protective_sl(pos)
        self._persist()

    async def _retry_fewer_lots(self, leg: DayLeg, direction: str, lots: int, reason: str) -> bool:
        """Funds/margin rejection: immediately re-enter with one lot fewer. True if retried."""
        if (not _FUNDS_REJECT.search(reason or "") or lots <= max(1, self.cfg.min_lots)
                or leg.funds_retries >= FUNDS_RETRY_MAX):
            return False
        leg.funds_retries += 1
        leg.lot_cap = lots - 1
        leg.status = "watching"
        leg.signal = None
        leg.error = ""
        self._log(f"{leg.index_key}: entry rejected for funds ({reason.strip()[:120]}) - "
                  f"retrying NOW with {leg.lot_cap} lot(s) "
                  f"(retry {leg.funds_retries}/{FUNDS_RETRY_MAX})", level="warn", index=leg.index_key)
        await self._enter(leg, direction)
        return True

    async def _retry_after_delayed_reject(self, pos: Position) -> bool:
        """Funds REJECTED status found later (reconcile/order stream), not at placement."""
        leg = self._legs.get(pos.index_key)
        if leg is None or leg.status != "error" or not self.enabled or pos.dry_run:
            return False
        now = now_ist()
        if (now.hour * 60 + now.minute >= self._cutoff_min(now)
                or time.time() - (pos.entry_ts or 0) > FUNDS_RETRY_WINDOW):
            return False
        return await self._retry_fewer_lots(leg, pos.option_type, pos.lots, leg.note)

    async def _refresh_entry(self, pos: Position) -> None:
        order = await self._order_state(pos.order_id, pos.entry_correlation_id)
        pos.order_id = str(order.get("orderId") or pos.order_id or "") or None
        status = str(order["orderStatus"]).upper()
        filled = int(order.get("filledQty") or 0)
        average = _as_float(order.get("averageTradedPrice") or order.get("averagePrice"))
        if status == "TRADED" and filled == 0:
            filled = int(order.get("quantity") or 0)
        if status == "PART_TRADED":
            if pos.order_id and not pos.entry_cancel_requested:
                pos.entry_cancel_requested = True
                self._persist()
                await get_dhan_client().cancel_order(pos.order_id)
            pos.execution_error = "Partial entry: cancelling remainder; waiting for terminal broker status."
        elif status in TERMINAL_ORDERS:
            if filled > 0 and average and average > 0:
                pos.quantity = filled
                pos.entry_quantity = filled
                pos.entry_premium = average
                pos.premium = feed.price(pos.security_id, self._segment_for(pos.index_key)) or average
                pos.execution_state = "open"
                pos.entry_confirmed_ts = time.time()
                pos.execution_error = ""
                t0 = getattr(self, "_signal_t0", None)
                self._log(f"Entry confirmed: {pos.index_key} {int(pos.strike)} "
                          f"{'CE' if pos.option_type == 'CALL' else 'PE'} {filled} units @ {average:.2f}"
                          f"{f' (+{(time.perf_counter() - t0) * 1000:.0f}ms since signal)' if t0 else ''}"
                          f" | next: watching profit/loss rule + target, exit {self._sess_val('exit_time')}",
                          level="success", notification="algo_entry")
                if self.cfg.protective_sl:
                    await self._place_protective_sl(pos)
            elif status != "TRADED" and filled == 0:
                leg = self._legs.get(pos.index_key)
                reason = str(order.get('omsErrorDescription') or 'no fill')
                if leg:
                    leg.status = "error"
                    leg.note = f"Entry {status}: {reason}"
                self._log(f"Entry {status}: {reason[:200]}; no position opened. "
                          f"ACTION: check Dhan, enter manually if needed.", level="error")
                self._position = None
            else:
                pos.execution_error = "Broker fill details incomplete; refusing to assume quantity or price."
        self._persist()

    async def _size_position(self, leg: DayLeg, balance: Optional[float] = None) -> Dict[str, Any]:
        """Size using policy-specific risk, available cash and the lot bounds.

        Target-only estimates 70% premium loss (100% without protection).
        Classic mode uses spot-stop distance x delta. The minimum-lot floor can
        exceed percentage budgets but never actual cash; neither is a loss cap.
        """
        lot = lot_size_for(leg.index_key)
        premium = leg.option_ltp or 0.0
        rng = leg.or_range or 0.0
        sl_points = self.cfg.sl_mult * rng
        delta = self._delta_for(leg, leg.signal) or get_settings().algo_paper_delta
        balance = await self._balance() if balance is None else balance
        risk_per_lot = max(
            premium * (ALGO_PROTECTIVE_SL_PCT / 100 if self.cfg.protective_sl else 1) * lot
            if self.cfg.target_only else sl_points * delta * lot, 1e-6,
        )
        premium_per_lot = max(premium * lot, 1e-6)

        if self.cfg.max_affordable:
            # Deliberately ignores max_lots: buy every lot the balance affords. The only
            # bound is the exchange freeze, above which the whole order would be rejected.
            usable = balance * AFFORDABLE_BALANCE_PCT / 100.0
            affordable = int(usable // premium_per_lot) if premium > 0 else 0
            freeze = get_settings().algo_freeze_limits.get(leg.index_key.upper())
            freeze_lots = freeze // lot if freeze and lot > 0 else None
            lots = max(0, affordable if freeze_lots is None else min(affordable, freeze_lots))
            if lots < max(1, self.cfg.min_lots):
                lots = 0
            return {
                "lots": lots, "quantity": lots * lot, "lotSize": lot,
                "premiumPerLot": round(premium_per_lot, 2), "riskPerLot": round(premium_per_lot, 2),
                "balance": round(balance, 2), "riskLots": affordable, "cashLots": affordable,
                "reason": (
                    f"max-affordable: balance Rs{balance:,.0f} ({AFFORDABLE_BALANCE_PCT:g}% usable), "
                    f"premium/lot Rs{premium_per_lot:,.0f} -> {lots} lot(s)"
                    + (f" (exchange freeze {freeze} qty per order)"
                       if freeze_lots is not None and affordable > freeze_lots else "")
                ) if lots else (
                    f"insufficient balance: Rs{balance:,.0f} available "
                    f"({AFFORDABLE_BALANCE_PCT:g}% usable), one lot of premium costs Rs{premium_per_lot:,.0f}"
                ),
            }

        risk_lots = int((balance * self.cfg.risk_pct / 100.0) // risk_per_lot)
        cash_lots = int((balance * self.cfg.max_capital_pct / 100.0) // premium_per_lot)

        # Minimum-lot floor. `min_lots` (default 1) is a hard floor on the RISK
        # cap, and a single lot is allowed whenever the balance can buy one
        # outright - even if the capital cap would rather it did not.
        floor = max(1, self.cfg.min_lots)
        risk_lots = max(floor, risk_lots)
        affordable_lots = int(balance // premium_per_lot)
        cash_lots = min(affordable_lots, max(floor, cash_lots))

        lots = max(0, min(risk_lots, cash_lots, self.cfg.max_lots))
        floored = lots >= floor and balance >= premium_per_lot \
            and (balance * self.cfg.risk_pct / 100.0) // risk_per_lot < floor

        return {
            "lots": lots,
            "quantity": lots * lot,
            "lotSize": lot,
            "premiumPerLot": round(premium_per_lot, 2),
            "riskPerLot": round(risk_per_lot, 2),
            "balance": round(balance, 2),
            "riskLots": risk_lots,
            "cashLots": cash_lots,
            "reason": (
                f"balance Rs{balance:,.0f}, premium/lot Rs{premium_per_lot:,.0f}, "
                f"risk/lot Rs{risk_per_lot:,.0f} -> {lots} lot(s)"
                + (" (minimum lot enforced)" if floored else "")
            ) if lots else (
                f"insufficient balance: Rs{balance:,.0f} available, but one lot of "
                f"premium costs Rs{premium_per_lot:,.0f} "
                f"(risk budget Rs{balance * self.cfg.risk_pct / 100.0:,.0f})"
            ),
        }

    def _delta_for(self, leg: DayLeg, direction: Optional[str] = None) -> Optional[float]:
        """Option delta for the leg we are about to trade.

        `direction` must be explicit; falling back to "CALL" silently looked up
        the wrong side on PUT entries.
        """
        side = direction or leg.prepared_side or leg.signal or "CALL"
        snap = chain_engine.snapshot(leg.index_key)
        if not snap:
            return None
        for row in snap.get("rows") or []:
            if abs(float(row["strike"]) - float(leg.strike or 0)) < 1e-6:
                node = row["ce" if side == "CALL" else "pe"] or {}
                d = (node.get("greeks") or {}).get("delta")
                return abs(float(d)) if d else None
        return None

    async def _balance(self) -> float:
        if self._is_dry():
            base = getattr(self, "_paper_balance", None)
            if base is None:
                base = 100000.0
            return float(base)
        try:
            f = await get_dhan_client().get_fund_limit()
            raw = f.get("availabelBalance") if isinstance(f, dict) else None
            try:
                balance = float(raw)
            except (ValueError, TypeError) as exc:
                raise DhanAPIError(0, "BAD_FUNDS", "Broker available balance is missing/invalid") from exc
            if not math.isfinite(balance) or balance < 0:
                raise DhanAPIError(0, "BAD_FUNDS", "Broker available balance is invalid")
            return balance
        except DhanAPIError as exc:
            self._last_error = f"Cannot size entry: broker balance unavailable: {exc.error_message}"
            self._log(self._last_error, level="error")
            raise

    # --------------------------------------------------------------- orders
    def _protective_trigger(self, pos: Position) -> float:
        if self.cfg.target_only:
            return max(.05, _round_tick(pos.entry_premium * (1 - ALGO_PROTECTIVE_SL_PCT / 100)))
        distance = abs(pos.entry_spot - pos.initial_stop_spot) * self.cfg.protective_sl_mult
        return max(.05, _round_tick(pos.entry_premium - distance * get_settings().algo_paper_delta))

    async def _migrate_protective_policy(self, pos: Position) -> bool:
        if not await self._cancel_protective(pos):
            pos.execution_error = "Target-only migration waiting for old SL cancellation. Check Dhan; old SL may still execute."
            self._persist()
            return False
        if pos.exit_order_id or pos.exit_correlation_id:
            return False
        if pos.dry_run:
            pos.next_exit_try_at = 0
        pos.protective_policy_pending = False
        pos.execution_error = ""
        self._log("Target-only policy active: no spot/break-even SL; broker protection uses a 70%-away premium trigger.",
                  level="warn")
        self._persist()
        return True

    def _segment_for(self, index_key: str) -> str:
        inst = get_index(index_key)
        return inst.derivative_segment if inst else "NSE_FNO"

    async def _send_order(
        self, *, side: str, security_id: int, qty: int, segment: str,
        order_type: str = "MARKET", price: float = 0.0, trigger: float = 0.0,
        correlation_id: str, tag: str, dry_run: Optional[bool] = None,
    ) -> Dict[str, Any]:
        s = get_settings()
        payload = {
            "dhanClientId": get_dhan_client()._client_id if self.account_id is not None else s.dhan_client_id,
            "transactionType": side,
            "exchangeSegment": segment,
            "productType": self.cfg.product_type,
            "orderType": order_type,
            "validity": "DAY",
            "securityId": str(security_id),
            "quantity": int(qty),
            "price": round(round(price / .05) * .05, 2) if order_type == "LIMIT" else 0.0,
            "triggerPrice": float(trigger),
            "disclosedQuantity": 0,
            "afterMarketOrder": False,
            "correlationId": correlation_id,
        }
        simulate = self._is_dry() if dry_run is None else dry_run
        if simulate:
            audit.log_order(action=f"algo_{tag}", success=True, dry_run=True, status="DRY_RUN",
                            message=f"[algo] {tag} {side} x{qty} (dry-run)", payload=payload)
            return {"ok": True, "dryRun": True, "orderId": f"DRY-{correlation_id}",
                    "fillPrice": None}
        try:
            res = await get_dhan_client().place_order(payload)
        except DhanAPIError as exc:
            audit.log_order(action=f"algo_{tag}", success=False, dry_run=False,
                            status=exc.error_code, message=exc.error_message, payload=payload)
            self._last_error = exc.error_message
            return {"ok": False, "error": exc.error_message,
                    "code": exc.error_code, "httpStatus": exc.status_code,
                    "uncertain": exc.status_code == 0 or exc.status_code >= 500}
        order_id = str(res.get("orderId") or "")
        audit.log_order(action=f"algo_{tag}", success=True, dry_run=False, order_id=order_id,
                        status=str(res.get("orderStatus")), message=f"[algo] {tag}",
                        payload=payload)
        return {"ok": True, "orderId": order_id or None,
                "status": res.get("orderStatus")}

    async def _place_protective_sl(self, pos: Position) -> None:
        """Park a WIDE real SL at Dhan so a server crash can't leave us naked."""
        if pos.protective_order_id:
            return
        if pos.protective_correlation_id:
            try:
                order = await self._order_state(None, pos.protective_correlation_id)
                pos.protective_order_id = str(order.get("orderId") or "") or None
            except DhanAPIError as exc:
                pos.execution_error = f"Protective SL outcome unknown: {exc.error_message}. Check Dhan."
                self._log(pos.execution_error, level="error")
            self._persist()
            return
        trigger_premium = self._protective_trigger(pos)
        live_premium = feed.price(pos.security_id, self._segment_for(pos.index_key))
        age = feed.age(pos.security_id, self._segment_for(pos.index_key))
        if not pos.dry_run and (live_premium is None or age is None or age > MAX_PRICE_AGE):
            pos.execution_error = "Waiting for fresh option price before parking protective SL."
            self._log(pos.execution_error, level="warn")
            return
        if live_premium is not None and trigger_premium >= live_premium:
            pos.execution_error = "Protective trigger already crossed; requesting safe market exit."
            self._log(pos.execution_error, level="error")
            await self._exit_position_locked(pos, "PROTECTIVE_LEVEL")
            return
        pos.protective_correlation_id = f"{self.ORDER_TAG}-S-{uuid.uuid4().hex[:18]}"
        pos.protective_cancel_requested = False
        self._persist()
        res = await self._send_order(
            side="SELL", security_id=pos.security_id, qty=pos.quantity,
            segment=self._segment_for(pos.index_key), order_type="STOP_LOSS_MARKET",
            trigger=trigger_premium,
            correlation_id=pos.protective_correlation_id, tag="protective_sl", dry_run=pos.dry_run,
        )
        if res.get("ok"):
            pos.protective_order_id = res.get("orderId")
            self._log(
                f"{pos.index_key}: disaster stop parked at Dhan @ premium {trigger_premium:.1f} "
                f"({'simulated' if res.get('dryRun') else 'submitted; fill not assumed'})", level="info")
            if not pos.dry_run and pos.protective_order_id:
                order = await self._order_state(pos.protective_order_id, None)
                status = str(order["orderStatus"]).upper()
                if status in {"REJECTED", "CANCELLED", "EXPIRED"}:
                    reason = str(order.get("omsErrorDescription") or order.get("omsErrorCode") or "no reason given")
                    pos.execution_error = (f"Protective SL {status} ({reason[:160]}); "
                                           f"broker SL NOT active - algo still watches exits.")
                    pos.execution_state = "attention"
                    self._log(pos.execution_error, level="error")
                else:
                    pos.execution_error = ""
        else:
            if not res.get("uncertain"):
                pos.protective_correlation_id = None
                pos.execution_state = "attention"
            pos.execution_error = f"Protective SL FAILED - {res.get('error')}"
            self._log(f"{pos.index_key}: protective SL FAILED - {res.get('error')}", level="error")
        self._persist()

    async def _cancel_protective(self, pos: Position) -> bool:
        if pos.dry_run:
            pos.protective_order_id = pos.protective_correlation_id = None
            return True
        if not pos.protective_order_id and not pos.protective_correlation_id:
            return True
        try:
            order = await self._order_state(pos.protective_order_id, pos.protective_correlation_id)
            order_id = str(order.get("orderId") or pos.protective_order_id or "")
            status = str(order["orderStatus"]).upper()
            if status not in TERMINAL_ORDERS:
                if not order_id:
                    raise DhanAPIError(0, "UNKNOWN_ORDER", "Cannot cancel protective stop without order ID.")
                if not pos.protective_cancel_requested:
                    pos.protective_cancel_requested = True
                    self._persist()
                    await get_dhan_client().cancel_order(order_id)
                order = await self._order_state(order_id, None)
                status = str(order["orderStatus"]).upper()
            if status not in TERMINAL_ORDERS:
                pos.execution_error = f"Protective SL cancellation not confirmed ({status}); no market SELL sent."
                pos.next_exit_try_at = time.time() + RECONCILE_SECONDS
                self._persist()
                return False
            pos.protective_order_id = pos.protective_correlation_id = None
            if status == "TRADED":
                pos.broker_exit_premium = _as_float(order.get("averageTradedPrice"))
                pos.exit_order_id = order_id
                pos.exit_reason = "PROTECTIVE_SL"
                pos.execution_state = "exit_pending"
                pos.execution_error = ""
            elif int(order.get("filledQty") or 0) > 0:
                filled = int(order["filledQty"])
                if not pos.entry_quantity:
                    pos.entry_quantity = pos.quantity
                pos.quantity = max(0, pos.quantity - filled)
                pos.partial_exit = True
                pos.next_exit_try_at = time.time() + RECONCILE_SECONDS
            self._persist()
            return True
        except DhanAPIError as exc:
            pos.execution_error = f"Cannot verify/cancel protective SL: {exc.error_message}; no duplicate SELL."
            if 0 < exc.status_code < 500:
                pos.protective_cancel_requested = False
            pos.next_exit_try_at = time.time() + RECONCILE_SECONDS
            self._log(pos.execution_error, level="error")
            self._persist()
            return False

    # ----------------------------------------------------------------- exit
    async def _exit_position(self, pos: Position, reason: str) -> None:
        async with self._execution_lock:
            await self._exit_position_locked(pos, reason)

    async def _exit_position_locked(self, pos: Position, reason: str) -> None:
        if self._position is not pos:
            return
        if pos.execution_state == "entry_pending":
            try:
                order = await self._order_state(pos.order_id, pos.entry_correlation_id)
                if str(order["orderStatus"]).upper() not in TERMINAL_ORDERS:
                    order_id = str(order.get("orderId") or pos.order_id or "")
                    if not order_id:
                        raise DhanAPIError(0, "UNKNOWN_ORDER", "Entry identity not confirmed; check Dhan.")
                    pos.entry_cancel_requested = True
                    self._persist()
                    await get_dhan_client().cancel_order(order_id)
                await self._refresh_entry(pos)
            except DhanAPIError as exc:
                pos.execution_error = f"Entry cancellation not confirmed: {exc.error_message}. Check Dhan."
                self._log(pos.execution_error, level="error")
            if self._position is not pos or pos.execution_state == "entry_pending":
                return
        if pos.exit_order_id or pos.exit_correlation_id or time.time() < pos.next_exit_try_at:
            return
        pos.exit_reason = reason
        if not await self._cancel_protective(pos):
            return
        if pos.exit_order_id or time.time() < pos.next_exit_try_at:
            await self._reconcile_locked()
            return
        if not pos.dry_run:
            try:
                rows = await get_dhan_client().get_positions(fresh=True)
            except DhanAPIError as exc:
                pos.execution_error = f"Exit blocked: broker holdings unknown: {exc.error_message}"
                self._log(pos.execution_error, level="error")
                return
            held = sum(int(r.get("netQty") or 0) for r in rows
                       if str(r.get("securityId")) == str(pos.security_id)
                       and r.get("exchangeSegment") == self._segment_for(pos.index_key)
                       and r.get("productType") == self.cfg.product_type)
            if held == 0:
                await self._reconcile_locked()
                return
            if held < 0 or held > pos.quantity:
                pos.execution_state = "attention"
                pos.execution_error = f"Unsafe exit quantity: broker {held}, tracked {pos.quantity}. Check Dhan."
                self._log(pos.execution_error, level="error")
                self._persist()
                return
            qty = held
            pos.partial_exit = pos.partial_exit or held != pos.quantity
        else:
            qty = pos.quantity
        pos.execution_state = "exit_pending"
        pos.exit_correlation_id = f"{self.ORDER_TAG}-X-{uuid.uuid4().hex[:18]}"
        self._persist()
        res = await self._send_order(
            side="SELL", security_id=pos.security_id, qty=qty,
            segment=self._segment_for(pos.index_key), order_type="MARKET",
            correlation_id=pos.exit_correlation_id, tag="exit", dry_run=pos.dry_run,
        )
        pos.exit_order_id = res.get("orderId")
        if not res.get("ok"):
            pos.execution_error = f"Exit not confirmed: {res.get('error')}; position remains tracked."
            self._log(pos.execution_error, level="error")
            if not res.get("uncertain"):
                pos.exit_order_id = pos.exit_correlation_id = None
                pos.execution_state = "open"
                pos.next_exit_try_at = time.time() + 3.0
        elif pos.dry_run:
            self._finalise_position(pos, pos.premium or pos.entry_premium, reason)
        else:
            self._log(f"Exit submitted ({pos.exit_order_id}); waiting for broker flat confirmation.")
        self._persist()

    def _trade_pnl(self, pos: Position, exit_premium: float) -> float:
        return (exit_premium - pos.entry_premium) * (pos.entry_quantity or pos.quantity)

    def _finalise_position(
        self, pos: Position, exit_premium: float, reason: str, estimated: bool = False
    ) -> None:
        if self._position is not pos:
            return
        quantity = pos.entry_quantity or pos.quantity
        gross_premium_pnl = self._trade_pnl(pos, exit_premium)
        trade = {
            "date": self._day, "index": pos.index_key, "direction": pos.option_type,
            "strike": pos.strike, "expiry": pos.expiry, "lots": pos.lots,
            "quantity": quantity,
            "entryTime": pos.entry_time, "exitTime": now_ist().strftime("%H:%M:%S"),
            "entrySpot": round(pos.entry_spot, 2), "exitSpot": round(pos.spot or pos.entry_spot, 2),
            "entryPremium": round(pos.entry_premium, 2), "exitPremium": round(exit_premium, 2),
            "targetSpot": pos.target_spot, "stopSpot": pos.stop_spot,
            "orRange": round(pos.or_range, 2),
            "points": round(pos.pnl_points, 2), "rMultiple": round(pos.r_multiple, 2),
            "mfe": round(pos.mfe, 2), "mae": round(pos.mae, 2),
            "premiumPnl": round(gross_premium_pnl, 2),
            "exitReason": reason, "dryRun": pos.dry_run,
            "targetOnly": self.cfg.target_only,
            "exitPriceEstimated": estimated,
        }
        self._today_trades.append(trade)
        self._store.append_trade(trade)
        if pos.dry_run:
            self._paper_balance = getattr(self, "_paper_balance", 100000.0) + gross_premium_pnl
        level = "success" if exit_premium >= pos.entry_premium else "warn"
        self._log(
            f"EXIT {pos.index_key} {pos.option_type} {int(pos.strike)} @ {exit_premium:.2f} "
            f"({reason})  spot {pos.pnl_points:+,.1f} pts  R {pos.r_multiple:+.2f}  "
            f"premium P&L Rs{gross_premium_pnl:,.0f}"
            + ("  [estimated exit price]" if estimated else ""),
            level=level, notification="algo_exit", trade=trade,
        )
        leg = self._legs.get(pos.index_key)
        if leg:
            leg.status = "closed"
        self._position = None
        self.phase = "day_done"
        self._persist()

    # --------------------------------------------------------------- status
    def _phase_label(self) -> str:
        return {
            "off": "OFF - algo is not armed",
            "preopen": "Pre-open - waiting for 09:15",
            "building_or": "Building opening range (09:15-09:24:59)",
            "watching": "Watching both indices for a breakout",
            "in_trade": "In a trade - managing stop & target",
            "day_done": "Done for today (trade taken or no breakout)",
            "closed": "Market closed",
            "entry_pending": "Entry submitted - waiting for confirmed broker fill",
            "exit_pending": "Exit submitted - waiting for broker flat confirmation",
            "attention": "Position needs attention - check execution warning",
        }.get(self.phase, self.phase)

    def _next_actions(self) -> List[str]:
        out: List[str] = []
        now = now_ist()
        minute = now.hour * 60 + now.minute
        if self._position is not None:
            p = self._position
            if p.execution_state == "entry_pending":
                return ["Waiting for broker entry fill/cancellation; no protective SELL until filled.",
                        "PANIC can request cancellation of the pending entry. Check Dhan if outcome is unknown."]
            if p.execution_state == "exit_pending":
                return ["Exit submitted or protective stop filled; waiting for broker flat confirmation.",
                        "No duplicate SELL will be submitted while the outcome is unresolved."]
            protection = (f"Far broker premium SL {self._protective_trigger(p):.2f} (70% away)."
                          if self.cfg.protective_sl else "Broker protective SL OFF.")
            out.append(
                f"Target-only: watching spot target {p.target_spot:,.1f}; software spot/break-even SL OFF. "
                f"{protection}"
                if self.cfg.target_only else
                f"Watching spot against target {p.target_spot:,.1f} and stop {p.stop_spot:,.1f}."
            )
            if not self.cfg.target_only and not p.breakeven_done and self.cfg.breakeven_at > 0:
                be = p.entry_spot + (self.cfg.breakeven_at * p.or_range
                                     * (1 if p.option_type == "CALL" else -1))
                out.append(f"Stop moves to break-even when spot touches {be:,.1f}.")
            out.append(f"Forced square-off at {self._sess_val('exit_time')} unless target/stop hits first.")
            return out
        if not self.enabled:
            out.append("Flip the switch ON to arm new entries.")
            return out
        sess = session_for(now)
        if sess.special:
            out.append(f"{sess.special['name']} session: OR {_hhmm(sess.open_min)}-{_hhmm(sess.or_end_min)}:59, "
                       f"{self._max_trades()} trade, entries till {self._sess_val('entry_cutoff')}, "
                       f"exit {self._sess_val('exit_time')}.")
        if minute < sess.open_min:
            out.append(f"Waiting for the {_hhmm(sess.open_min)} open.")
        elif minute < sess.or_ready_min:
            out.append(f"Will lock the opening range ({_hhmm(sess.open_min)}-{_hhmm(sess.or_end_min)}:59) "
                       f"at {_hhmm(sess.or_ready_min)} and pre-pick contracts.")
        watching = [k for k, l in self._legs.items() if l.status == "watching"]
        if watching:
            out.append(f"Watching {', '.join(watching)} for the first break of the range.")
            out.append("First index to break takes the trade; the other is locked out for the day.")
        if minute >= self._cutoff_min(now) and not self._today_trades:
            out.append(f"No fresh entries after {self._sess_val('entry_cutoff')}. Trade window closed.")
        if self._today_trades:
            done = len(self._today_trades)
            out.append(f"{done}/{self._max_trades()} trade(s) done today."
                       + (" Waiting for the next session." if done >= self._max_trades() else
                          " Next trade needs every account flat and a fresh breakout."))
        return out

    def status(self) -> Dict[str, Any]:
        now = now_ist()
        pos = self._position
        legs = []
        for key in self.cfg.indices:
            leg = self._legs.get(key) or DayLeg(key)
            spot = leg.spot or 0.0
            legs.append({
                "index": key,
                "name": (get_index(key).name if get_index(key) else key),
                "status": leg.status,
                "orHigh": leg.or_high,
                "orLow": leg.or_low,
                "orRange": leg.or_range,
                "spot": round(spot, 2) if spot else None,
                "toUpper": round(leg.or_high - spot, 2) if (leg.or_high and spot) else None,
                "toLower": round(spot - leg.or_low, 2) if (leg.or_low and spot) else None,
                "signal": leg.signal,
                "strike": leg.strike,
                "securityId": leg.security_id,
                "expiry": leg.expiry,
                "optionLtp": leg.option_ltp,
                "plannedLots": leg.planned_lots,
                "note": leg.note,
            })
        return {
            "enabled": self.enabled,
            "phase": self.phase,
            "phaseLabel": self._phase_label(),
            "dryRun": self._is_dry(),
            "serverTime": now.strftime("%Y-%m-%d %H:%M:%S"),
            "serverTimeShort": now.strftime("%H:%M:%S"),
            "day": self._day,
            "marketOpen": (session_for(now).trading_day
                           and session_for(now).open_min <= now.hour * 60 + now.minute < session_for(now).close_min),
            "config": asdict(self.cfg),
            "legs": legs,
            "position": self._position_dict(),
            "nextActions": self._next_actions(),
            "todayTrades": self._today_trades,
            "stats": self._stats(),
            "feed": feed.status(),
            "recovered": self._recovered,
            "lastError": self._last_error,
            "brokerSyncAt": self._broker_sync_at,
            "brokerSyncError": self._broker_sync_error,
            "brokerNetQty": self._broker_net_qty,
            "brokerPositionPnl": self._broker_position_pnl,
            "paperBalance": getattr(self, "_paper_balance", None),
            "uptimeSeconds": round(time.time() - self._started_at, 0),
            "events": self._events[-60:],
        }

    def _position_dict(self) -> Optional[Dict[str, Any]]:
        p = self._position
        if p is None:
            return None
        return {
            "index": p.index_key,
            "optionType": p.option_type,
            "strike": p.strike,
            "securityId": p.security_id,
            "expiry": p.expiry,
            "lots": p.lots,
            "quantity": p.quantity,
            "entrySpot": round(p.entry_spot, 2),
            "spot": round(p.spot, 2) if p.spot else None,
            "entryPremium": round(p.entry_premium, 2),
            "premium": round(p.premium, 2) if p.premium else None,
            "entryTime": p.entry_time,
            "targetSpot": p.target_spot,
            "stopSpot": p.stop_spot,
            "softwareStopEnabled": not self.cfg.target_only,
            "protectiveTrigger": self._protective_trigger(p) if self.cfg.protective_sl else None,
            "initialStopSpot": p.initial_stop_spot,
            "breakevenDone": p.breakeven_done,
            "points": round(p.pnl_points, 2),
            "rMultiple": round(p.r_multiple, 2),
            "premiumPnl": round(p.pnl_premium, 0),
            "mfe": round(p.mfe, 2),
            "mae": round(p.mae, 2),
            "dryRun": p.dry_run,
            "orderId": p.order_id,
            "protectiveOrderId": p.protective_order_id,
            "executionState": p.execution_state,
            "executionError": p.execution_error,
            "exitOrderId": p.exit_order_id,
            "spotAgeSeconds": feed.age(get_index(p.index_key).sec_id, "IDX_I") if get_index(p.index_key) else None,
        }

    def _stats(self) -> Dict[str, Any]:
        closed = self._today_trades
        wins = [t for t in closed if t.get("premiumPnl", 0) > 0]
        week = self._store.recent_trades(days=7)
        return {
            "todayTrades": len(closed),
            "todayPnl": round(sum(t.get("premiumPnl", 0) for t in closed), 0),
            "todayR": round(sum(t.get("rMultiple", 0) for t in closed), 2),
            "todayWins": len(wins),
            "weekTrades": len(week),
            "weekPnl": round(sum(t.get("premiumPnl", 0) for t in week), 0),
            "weekR": round(sum(t.get("rMultiple", 0) for t in week), 2),
            "allTimePnl": round(sum(t.get("premiumPnl", 0) for t in self._store.read_trades(limit=5000)), 0),
        }


def _as_float(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _as_int(v: Any) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


class AccountEngines:
    def __init__(self) -> None:
        self.items = {account: AlgoEngine(account) for account in ACCOUNT_IDS}
        self.selection_lock = asyncio.Lock()
        self.entry_accounts: tuple[AccountId, ...] = ()

    def current(self) -> AlgoEngine:
        return self.items[current_account()]

    async def start(self) -> None:
        self.entry_accounts = ()
        feed.set_on_tick(self.on_tick)
        await feed.start()
        for account, engine in self.items.items():
            with account_scope(account):
                # Restore management for both, never silently resume new entries.
                await engine.start()
                engine.enabled = False
                engine._persist()
                if all(account_credentials(account)):
                    get_dhan_client().start_live_sync()

    async def stop(self) -> None:
        for account, engine in self.items.items():
            with account_scope(account):
                await engine.stop()
        await feed.stop()

    def on_tick(self, sid: int, price: float, segment: str) -> None:
        for account, engine in self.items.items():
            with account_scope(account):
                engine._on_tick(sid, price, segment)

    async def enter_signal(self, source: DayLeg, direction: str) -> None:
        t0 = time.perf_counter()
        master = self.items["master"]
        if not self.entry_accounts:
            return
        if source.prepared_side != direction or not source.security_id:
            await master._prepare_contract(source, direction)
        if source.prepared_side != direction or not source.security_id:
            return
        # The contract is often subscribed at the breakout tick itself, so its first
        # premium tick may not have arrived yet. Do not consume the day's single
        # signal on a stale premium; the next tick retries while the leg is watching.
        segment = master._segment_for(source.index_key)
        premium_age = feed.age(source.security_id, segment)
        if (feed.price(source.security_id, segment) is None
                or premium_age is None or premium_age > MAX_PRICE_AGE):
            note = "Waiting for fresh option premium before entry (maximum age 5s)"
            if source.note != note:
                master._log(f"{source.index_key} {direction} breakout: {note}; will retry on next tick",
                            level="warn", index=source.index_key)
            source.note = note
            return
        signal = asdict(source)
        today = now_ist().date().isoformat()
        prev_signal = (master._signal_day, master._signal_count)
        trade_no = master._signals_used(today) + 1
        master._log(f"SIGNAL {source.index_key} {direction} (trade {trade_no}/{master._max_trades()}): "
                    f"spot {source.spot} vs OR "
                    f"H {source.or_high} / L {source.or_low}; contract {int(source.strike or 0)} "
                    f"{'CE' if direction == 'CALL' else 'PE'} premium {feed.price(source.security_id, segment)} "
                    f"-> sending to {','.join(self.entry_accounts)} "
                    f"(+{(time.perf_counter() - t0) * 1000:.0f}ms)", index=source.index_key)
        master._signal_day = today
        master._signal_count = trade_no
        with account_scope("master"):
            master._persist()
        # Accounts where an order may have reached the broker (or the outcome is
        # unknown). Only these consume the day's single signal.
        consumed: set = set()
        async def enter(account: AccountId) -> None:
            engine = self.items[account]
            with account_scope(account):
                try:
                    if engine._position is not None or len(engine._today_trades) >= engine._max_trades():
                        consumed.add(account)
                        engine._log("Shared signal skipped: existing position/trade.", level="warn")
                        return
                    leg = DayLeg(**signal)
                    engine._legs[source.index_key] = leg
                    engine._signal_t0 = t0
                    if engine is master:
                        await engine._enter(leg, direction)
                    else:
                        async with engine._execution_lock:
                            leg = DayLeg(**signal)
                            engine._legs[source.index_key] = leg
                            await engine._enter(leg, direction)
                    if (engine._position is not None or leg.entry_attempts > 0
                            or leg.status in ("active", "error")):
                        consumed.add(account)
                    if engine._position is None:
                        engine._last_error = leg.error or leg.note or "Signal did not open a position; inspect journal."
                    engine._log(f"shared_signal direction={direction} index={source.index_key} "
                                f"result={engine._position.execution_state if engine._position else leg.note}",
                                level="info" if engine._position else "warn")
                except Exception as exc:
                    consumed.add(account)
                    engine._last_error = str(exc)
                    engine._log(f"Shared signal failed: {exc}", level="error")
                    logger.exception("account=%s shared signal failed", account)
                finally:
                    try:
                        engine._persist()
                    except OSError as exc:
                        engine._last_error = f"State persistence failed: {exc}. Check this account at Dhan."
                        logger.exception("account=%s signal state persistence failed", account)
        await asyncio.gather(*(enter(account) for account in self.entry_accounts))
        if not consumed:
            # 2026-11-04: spot dipped back under the OR high during pre-order checks,
            # every account aborted before sending an order, yet the day stayed locked.
            master._signal_day, master._signal_count = prev_signal
            leg = master._legs.get(source.index_key, source)
            if leg.status == "watching":
                leg.next_try_at = time.time() + SIGNAL_RETRY_COOLDOWN
            master._log(f"{source.index_key} {direction} breakout: no account sent an order "
                        f"({leg.note or 'pre-order check failed'}); signal NOT consumed, "
                        f"watching for the next valid breakout", level="warn", index=source.index_key)
            with account_scope("master"):
                master._persist()
            return
        source.status = "locked"
        source.note = "Shared signal dispatched; inspect each account result. No blind replay."
        # This signal is spent: nothing else may fire until every account is flat
        # and _maybe_open_next_trade re-watches with the fresh-breakout guard.
        for other in master._legs.values():
            if other.status == "watching":
                other.status = "locked"
                other.note = f"{source.index_key} took trade {trade_no}"
        with account_scope("master"):
            master._persist()


engines = AccountEngines()


class AccountAlgo:
    def __getattr__(self, name: str):
        return getattr(engines.current(), name)


algo = AccountAlgo()
