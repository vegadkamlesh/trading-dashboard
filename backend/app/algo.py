"""ORB Algo — the automatic Opening-Range Breakout trader.

What it does, once you flip it ON
---------------------------------
  09:15-09:25  builds the opening range (OR high / OR low) for NIFTY + SENSEX
  09:25        locks the levels, pre-picks the option contract, sizes the lots
  after 09:25  watches BOTH indices tick-by-tick. The FIRST one to break its
               range takes the trade; the other is locked out for the day.
               break UP   -> buy CALL (ATM, or ATM +/- offset)
               break DOWN -> buy PUT
  in trade     manages on SPOT levels:
                   stop   = entry -/+ (sl_mult x OR range)
                   target = entry +/- (target_mult x OR range)
               and moves the stop to break-even once +breakeven x OR is reached
  15:12        force square-off
  15:30        day closed, everything archived to the journal

Safety model (read this)
------------------------
* DRY-RUN is the default. In dry-run NOTHING is sent to Dhan; fills are
  simulated from live prices so you can paper-trade the real signals.
* The primary stop/target are SPOT levels monitored in software, because the
  strategy is defined on spot. That is precise but depends on this process
  staying alive.
* To cover that, a SECOND, much wider stop is parked at Dhan as a real
  STOP_LOSS_MARKET order the moment the entry fills (protective_sl, on by
  default). If this server dies, you are not left naked.
* State is persisted to disk after every meaningful change and re-synced with
  Dhan's own position book on startup, so a crash/restart resumes mid-day.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from . import audit, runtime
from .algo_store import store
from .config import get_settings
from .dhan_client import DhanAPIError, get_dhan_client
from .feed import feed
from .instruments import get_index, lot_size_for
from .optionchain import engine as chain_engine
from .opening_range import WINDOW_END_MIN, WINDOW_START_MIN

logger = logging.getLogger("dhan.algo")

IST = timezone(timedelta(hours=5, minutes=30))
MARKET_OPEN_MIN = 9 * 60 + 15
MARKET_CLOSE_MIN = 15 * 60 + 30
OR_READY_MIN = 9 * 60 + 26          # start computing the OR at/after this minute
TICKER = 15

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
    sl_mult: float = 0.2            # stop   = sl_mult     x OR range
    target_mult: float = 2.0        # target = target_mult x OR range
    breakeven_at: float = 0.0       # move stop to entry after this many x OR (0=off)
    entry_cutoff: str = "15:00"     # no new entries after this IST time
    exit_time: str = "15:12"        # forced square-off
    strike_offset: int = 0          # 0 = ATM, +1 = one strike further OTM
    risk_pct: float = 0.5           # % of balance risked per trade (see Settings)
    max_capital_pct: float = 60.0   # max % of balance spent on premium
    max_lots: int = 10
    min_lots: int = 1
    min_or_pct: float = 0.0         # skip if OR range < this % of spot (0 = off)
    max_or_pct: float = 0.0         # skip if OR range > this % of spot (0 = off)
    order_type: str = "MARKET"
    product_type: str = "INTRADAY"
    protective_sl: bool = True      # park a wide real SL at Dhan as a disaster stop
    protective_sl_mult: float = 2.0 # ... at this many x the software stop distance

    @classmethod
    def from_settings(cls) -> "AlgoConfig":
        s = get_settings()
        return cls(
            indices=s.algo_indices or ["NIFTY", "SENSEX"],
            sl_mult=s.algo_sl_mult,
            target_mult=s.algo_target_mult,
            breakeven_at=s.algo_breakeven_at,
            entry_cutoff=s.algo_entry_cutoff,
            exit_time=s.algo_exit_time,
            strike_offset=s.algo_strike_offset,
            risk_pct=s.algo_effective_risk_pct,
            max_capital_pct=s.algo_max_capital_pct,
            max_lots=s.algo_max_lots,
            min_lots=s.algo_min_lots,
            min_or_pct=s.algo_min_or_pct,
            max_or_pct=s.algo_max_or_pct,
            order_type=s.algo_order_type,
            product_type=s.algo_product_type,
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
    entry_attempts: int = 0
    next_try_at: float = 0.0        # epoch seconds; entry retried only after this
    error: str = ""

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
    def __init__(self) -> None:
        self.cfg = AlgoConfig.from_settings()
        self.enabled = False
        self.phase = "off"
        self._day: Optional[str] = None
        self._legs: Dict[str, DayLeg] = {}
        self._position: Optional[Position] = None
        self._events: List[Dict[str, Any]] = []
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._busy = False
        self._started_at = time.time()
        self._feed_connected = False
        self._last_error: Optional[str] = None
        self._today_trades: List[Dict[str, Any]] = []
        self._recovered: Optional[str] = None

    # ------------------------------------------------------------------ log
    def _log(self, msg: str, level: str = "info", **extra: Any) -> None:
        ev = {"kind": "algo", "level": level, "msg": msg, **extra}
        self._events.append(ev)
        if len(self._events) > 400:
            self._events = self._events[-400:]
        store.append_event(ev, day=self._day)
        getattr(logger, {"warn": "warning"}.get(level, level), logger.info)(msg)

    # ------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        # Rolling retention: drop journals older than 7 days.
        store.prune_journals()
        feed.set_on_tick(self._on_tick)
        await feed.start()
        self._restore()
        self._task = asyncio.create_task(self._supervisor(), name="algo-supervisor")
        logger.info("Algo engine started (enabled=%s, dryRun=%s)", self.enabled, runtime.is_dry_run())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        await feed.stop()
        self._persist()

    # ---------------------------------------------------------------- state
    def _persist(self) -> None:
        store.save_state(
            {
                "enabled": self.enabled,
                "phase": self.phase,
                "day": self._day,
                "config": asdict(self.cfg),
                "legs": {k: asdict(v) for k, v in self._legs.items()},
                "position": asdict(self._position) if self._position else None,
                "todayTrades": self._today_trades,
                "savedAt": time.time(),
            }
        )

    def _restore(self) -> None:
        """Reload the previous session and re-sync with Dhan's position book."""
        data = store.load_state()
        if not data:
            return
        try:
            cfg = AlgoConfig(**{**asdict(self.cfg), **(data.get("config") or {})})
            self.cfg = cfg
        except TypeError:
            pass
        self.enabled = bool(data.get("enabled"))
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
                self._position = Position(**pos)
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
        result: Dict[str, Any] = {"checkedAt": now_ist().strftime("%H:%M:%S")}
        if not get_settings().is_configured():
            result["skipped"] = "Dhan not configured"
            return result
        try:
            rows = await get_dhan_client().get_positions()
        except DhanAPIError as exc:
            result["error"] = exc.error_message
            self._last_error = exc.error_message
            return result
        net: Dict[str, int] = {}
        for r in rows or []:
            sid = str(r.get("securityId") or "")
            net[sid] = net.get(sid, 0) + int(r.get("netQty") or 0)
        result["openPositions"] = {k: v for k, v in net.items() if v}
        pos = self._position
        if pos is not None:
            held = net.get(str(pos.security_id), 0)
            if held <= 0:
                self._log(
                    f"Reconcile: {pos.index_key} {pos.option_type} no longer held at Dhan "
                    f"-> marking trade closed", level="warn")
                self._finalise_position(pos, pos.premium or pos.entry_premium, "RECONCILED")
            elif held != pos.quantity:
                self._log(
                    f"Reconcile: holding {held} but tracking {pos.quantity}; adopting Dhan qty",
                    level="warn")
                pos.quantity = held
        else:
            for sid, qty in net.items():
                if qty > 0:
                    self._log(
                        f"Reconcile: untracked position {sid} x{qty} found at Dhan "
                        f"- NOT managed by algo. Please square off manually if unwanted.",
                        level="error")
        result["tracked"] = bool(self._position)
        return result

    # ---------------------------------------------------------------- toggles
    async def arm(self) -> Dict[str, Any]:
        self.enabled = True
        self._log(f"ALGO ARMED ({'DRY-RUN' if runtime.is_dry_run() else 'LIVE'})", level="success")
        await self.reconcile()
        # Run one supervisor pass immediately so the returned status already
        # shows the real phase (otherwise the UI briefly says "OFF").
        await self._tick_supervisor()
        self._persist()
        return self.status()

    def disarm(self, reason: str = "operator") -> Dict[str, Any]:
        self.enabled = False
        self._log(f"ALGO DISARMED ({reason})"
                  + (" - open position left as-is" if self._position else ""), level="warn")
        self._persist()
        return self.status()

    async def panic_exit(self, reason: str = "PANIC button") -> Dict[str, Any]:
        self._log(f"PANIC EXIT requested: {reason}", level="error")
        if self._position is None:
            self._log("Panic exit: no open position", level="warn")
            return self.status()
        await self._exit_position(self._position, "PANIC")
        return self.status()

    def set_config(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        allowed = set(asdict(self.cfg).keys())
        for k, v in (patch or {}).items():
            if k in allowed and v is not None:
                current = getattr(self.cfg, k)
                try:
                    setattr(self.cfg, k, type(current)(v) if not isinstance(current, list)
                            else [str(x).upper() for x in v])
                except (TypeError, ValueError):
                    continue
        self._log("Configuration updated by operator")
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
                logger.exception("algo supervisor error")
            await asyncio.sleep(0.5)

    async def _tick_supervisor(self) -> None:
        now = now_ist()
        today = now.date().isoformat()
        minute = now.hour * 60 + now.minute
        ft = feed.status()
        self._feed_connected = bool(ft.get("webSocket"))

        if self._day != today:
            self._reset_day(today, now)

        if not self.enabled:
            self.phase = "off"
            return

        if now.weekday() >= 5:
            self.phase = "closed"
            return
        if minute < MARKET_OPEN_MIN:
            self.phase = "preopen"
            return
        if minute >= MARKET_CLOSE_MIN:
            self.phase = "closed"
            return

        # OR build + watch
        if minute >= OR_READY_MIN:
            await self._maybe_compute_or()

        # Forced square-off
        if self._position is not None and minute >= _to_min(self.cfg.exit_time, 15 * 60 + 12):
            await self._exit_position(self._position, "TIME")

        if self._position is not None:
            self.phase = "in_trade"
        elif any(l.status == "watching" for l in self._legs.values()):
            self.phase = "watching"
        elif minute < OR_READY_MIN:
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
        self.phase = "preopen" if now.hour < 9 else "building_or"
        self._persist()

    # -------------------------------------------------------------- OR build
    def _can_compute_or(self) -> bool:
        now = now_ist()
        if now.weekday() >= 5:
            return False
        return (now.hour * 60 + now.minute) >= OR_READY_MIN

    async def _intraday(self, **kwargs: Any) -> Dict[str, Any]:
        """Intraday fetch with throttle + 429 backoff (arming fires 2 back-to-back)."""
        client = get_dhan_client()
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
                    from_dt=f"{today.isoformat()} 09:15:00",
                    to_dt=f"{today.isoformat()} 09:26:00",
                )
            except DhanAPIError as exc:
                leg.note = f"OR fetch failed: {exc.error_message}"
                self._last_error = exc.error_message
                continue
            highs, lows, ts = [], [], []
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
                    if WINDOW_START_MIN <= m <= WINDOW_END_MIN and mv > 0 and lv > 0:
                        highs.append(mv)
                        lows.append(lv)
            if not highs or not lows:
                leg.note = "no opening-range candles yet"
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

    async def _prepare_contract(self, leg: DayLeg) -> None:
        """Pick the ATM option contract now, so entry needs zero lookups."""
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
        spot = float(snap.get("underlyingLtp") or 0) or (leg.spot or 0)
        step = self._strike_step(rows, inst.strike_step if inst else 50)
        if not spot or step <= 0:
            leg.note = "no spot / strike step"
            return
        leg.expiry = snap.get("expiry")
        leg.spot = spot
        for direction in ("CALL", "PUT"):
            strike = self._pick_strike(spot, step, direction, self.cfg.strike_offset)
            row = next((r for r in rows if abs(float(r["strike"]) - strike) < 1e-6), None)
            if row is None:
                continue
            node = row["ce" if direction == "CALL" else "pe"] or {}
            leg.strike = strike
            leg.security_id = _as_int(node.get("securityId"))
            leg.option_ltp = _as_float(node.get("ltp"))
            break
        if leg.security_id:
            feed.subscribe(leg.security_id, inst.derivative_segment if inst else "NSE_FNO")
            self._log(
                f"{leg.index_key} contract ready: {int(leg.strike)} "
                f"{'CE' if leg.strike else ''} (ATM{'+' if self.cfg.strike_offset > 0 else ''}"
                f"{self.cfg.strike_offset}) premium ~{leg.option_ltp}",
                level="info")

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
    def _on_tick(self, security_id: int, ltp: float) -> None:
        """Called from the feed on every price change. Must stay non-blocking."""
        for leg in self._legs.values():
            if leg.index_key in self.cfg.indices and get_index(leg.index_key) and \
                    get_index(leg.index_key).sec_id == security_id:
                leg.spot = ltp
                break
        if self._position is not None and self._position.security_id == security_id:
            self._position.premium = ltp
        if self.enabled and not self._busy:
            asyncio.ensure_future(self._evaluate())

    async def _evaluate(self) -> None:
        """Decide whether to enter or exit, right now."""
        self._busy = True
        try:
            if self._position is not None:
                await self._manage_position()
            else:
                await self._look_for_entry()
        finally:
            self._busy = False

    async def _manage_position(self) -> None:
        pos = self._position
        if pos is None or pos.spot is None:
            return
        d = pos.spot - pos.entry_spot
        moved = d if pos.option_type == "CALL" else -d
        pos.mfe = max(pos.mfe, moved)
        pos.mae = min(pos.mae, moved)

        # Break-even ratchet
        if (self.cfg.breakeven_at > 0 and not pos.breakeven_done
                and moved >= self.cfg.breakeven_at * pos.or_range):
            pos.breakeven_done = True
            pos.stop_spot = pos.entry_spot
            self._log(f"{pos.index_key}: +{moved:,.1f} pts reached -> stop moved to break-even",
                      level="success", index=pos.index_key)
            self._persist()

        hit_stop = (pos.spot <= pos.stop_spot) if pos.option_type == "CALL" else (pos.spot >= pos.stop_spot)
        hit_target = (pos.spot >= pos.target_spot) if pos.option_type == "CALL" else (pos.spot <= pos.target_spot)
        if hit_stop:
            await self._exit_position(pos, "BREAKEVEN" if pos.breakeven_done and
                                      abs(pos.stop_spot - pos.entry_spot) < 1e-6 else "STOP")
        elif hit_target:
            await self._exit_position(pos, "TARGET")

    async def _look_for_entry(self) -> None:
        now = now_ist()
        minute = now.hour * 60 + now.minute
        if minute > _to_min(self.cfg.entry_cutoff, 15 * 60):
            return
        for key in self.cfg.indices:
            leg = self._legs.get(key)
            if leg is None or leg.status != "watching" or leg.or_high is None or leg.spot is None:
                continue
            if time.time() < leg.next_try_at:
                continue   # backing off after a rejected entry
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
                await self._enter(leg, "CALL")
                return
            if leg.spot < leg.or_low:
                await self._enter(leg, "PUT")
                return

    # ---------------------------------------------------------------- entry
    async def _enter(self, leg: DayLeg, direction: str) -> None:
        rng = leg.or_range or 0.0
        spot = leg.spot or 0.0
        if rng <= 0 or spot <= 0:
            return
        await self._prepare_contract(leg)
        if not leg.security_id or leg.strike is None:
            leg.note = "contract not ready - cannot enter"
            self._log(f"{leg.index_key}: breakout seen but contract not ready", level="error")
            return

        sizing = await self._size_position(leg)
        if sizing["lots"] < max(1, self.cfg.min_lots):
            leg.status = "skipped"
            leg.note = sizing["reason"]
            self._log(f"{leg.index_key}: breakout skipped - {sizing['reason']}", level="warn")
            self._persist()
            return

        target = spot + self.cfg.target_mult * rng if direction == "CALL" \
            else spot - self.cfg.target_mult * rng
        stop = spot - self.cfg.sl_mult * rng if direction == "CALL" \
            else spot + self.cfg.sl_mult * rng

        pos = Position(
            index_key=leg.index_key, option_type=direction,
            security_id=leg.security_id, strike=float(leg.strike),
            expiry=leg.expiry or "", lots=sizing["lots"], quantity=sizing["quantity"],
            entry_spot=spot, entry_premium=leg.option_ltp or 0.0,
            entry_time=now_ist().strftime("%H:%M:%S"), entry_ts=time.time(),
            or_range=rng, target_spot=round(target, 2), stop_spot=round(stop, 2),
            initial_stop_spot=round(stop, 2), dry_run=runtime.is_dry_run(),
            spot=spot, premium=leg.option_ltp,
        )
        self._log(
            f"BREAKOUT {leg.index_key} {'UP' if direction == 'CALL' else 'DOWN'} @ {spot:,.1f} "
            f"-> BUY {direction} {int(leg.strike)} x{pos.quantity} ({pos.lots} lot)"
            f"{' [DRY-RUN]' if pos.dry_run else ''}",
            level="success", index=leg.index_key, direction=direction,
            strike=pos.strike, lots=pos.lots, spot=spot, target=pos.target_spot, stop=pos.stop_spot,
        )
        leg.status = "active"
        leg.signal = direction
        leg.planned_lots = sizing["lots"]

        # Place the entry
        fill = await self._send_order(
            side="BUY", security_id=pos.security_id, qty=pos.quantity,
            segment=self._segment_for(leg.index_key), order_type=self.cfg.order_type,
            correlation_id=f"ORB-{leg.index_key}-{self._day}-{uuid.uuid4().hex[:6]}",
            tag="entry",
        )
        if not fill.get("ok"):
            code = str(fill.get("code") or "")
            msg = str(fill.get("error") or "order failed")
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
        if fill.get("fillPrice"):
            pos.entry_premium = float(fill["fillPrice"])
        self._position = pos
        self.phase = "in_trade"

        # Lock out the other index (one trade per day).
        for other in self.cfg.indices:
            if other != leg.index_key and self._legs.get(other, DayLeg(other)).status == "watching":
                o = self._legs[other]
                o.status = "locked"
                o.note = f"{leg.index_key} took today's trade"

        if self.cfg.protective_sl:
            await self._place_protective_sl(pos)
        self._persist()

    async def _size_position(self, leg: DayLeg) -> Dict[str, Any]:
        """Lots from balance: risk-based, but NEVER fewer than min_lots (default 1).

        The risk budget is per STREAK, not per trade - this strategy wins ~15%
        of the time and 39-46 consecutive losses are normal. Scaling resolves to
        roughly Rs1,00,000 of capital per lot:

            Rs   10,000  ->  1 lot  (minimum enforced, if 1 lot is affordable)
            Rs   30,000  ->  1 lot
            Rs 1,00,000  ->  1 lot
            Rs 2,00,000  ->  2 lots
            Rs 4,00,000  ->  4 lots

        The floor is deliberate: the operator wants at least one lot whenever the
        balance can buy one outright, even if that is riskier than the streak
        budget would allow. When even one lot is unaffordable we return 0 lots
        with an "insufficient balance" reason and the caller skips the trade.

        Note the floor only replaces the RISK cap, never affordability -
        `cash_lots` still has the final say on whether the money is actually there.
        """
        lot = lot_size_for(leg.index_key)
        premium = leg.option_ltp or 0.0
        rng = leg.or_range or 0.0
        sl_points = self.cfg.sl_mult * rng
        delta = self._delta_for(leg) or get_settings().algo_paper_delta
        balance = await self._balance()
        risk_per_lot = max(sl_points * delta * lot, 1e-6)
        premium_per_lot = max(premium * lot, 1e-6)

        risk_lots = int((balance * self.cfg.risk_pct / 100.0) // risk_per_lot)
        cash_lots = int((balance * self.cfg.max_capital_pct / 100.0) // premium_per_lot)

        # Minimum-lot floor. `min_lots` (default 1) is a hard floor on the RISK
        # cap, and a single lot is allowed whenever the balance can buy one
        # outright - even if the capital cap would rather it did not.
        floor = max(1, self.cfg.min_lots)
        risk_lots = max(floor, risk_lots)
        if balance >= premium_per_lot:
            cash_lots = max(floor, cash_lots)

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

    def _delta_for(self, leg: DayLeg) -> Optional[float]:
        snap = chain_engine.snapshot(leg.index_key)
        if not snap:
            return None
        for row in snap.get("rows") or []:
            if abs(float(row["strike"]) - float(leg.strike or 0)) < 1e-6:
                node = row["ce" if (leg.signal or "CALL") == "CALL" else "pe"] or {}
                d = (node.get("greeks") or {}).get("delta")
                return abs(float(d)) if d else None
        return None

    async def _balance(self) -> float:
        if runtime.is_dry_run():
            base = getattr(self, "_paper_balance", None)
            if base is None:
                base = 100000.0
            return float(base)
        try:
            f = await get_dhan_client().get_fund_limit()
            raw = f.get("availabelBalance") if isinstance(f, dict) else None
            if raw is None and isinstance(f, dict):
                raw = f.get("sodLimit")
            return float(raw or 0.0)
        except DhanAPIError:
            return 0.0

    # --------------------------------------------------------------- orders
    def _segment_for(self, index_key: str) -> str:
        inst = get_index(index_key)
        return inst.derivative_segment if inst else "NSE_FNO"

    async def _send_order(
        self, *, side: str, security_id: int, qty: int, segment: str,
        order_type: str = "MARKET", price: float = 0.0, trigger: float = 0.0,
        correlation_id: str, tag: str,
    ) -> Dict[str, Any]:
        s = get_settings()
        payload = {
            "dhanClientId": s.dhan_client_id,
            "transactionType": side,
            "exchangeSegment": segment,
            "productType": self.cfg.product_type,
            "orderType": order_type,
            "validity": "DAY",
            "securityId": str(security_id),
            "quantity": int(qty),
            "price": 0.0 if order_type == "MARKET" else float(price),
            "triggerPrice": float(trigger),
            "disclosedQuantity": 0,
            "afterMarketOrder": False,
            "correlationId": correlation_id,
        }
        if runtime.is_dry_run():
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
                    "code": exc.error_code, "httpStatus": exc.status_code}
        order_id = str(res.get("orderId") or "")
        audit.log_order(action=f"algo_{tag}", success=True, dry_run=False, order_id=order_id,
                        status=str(res.get("orderStatus")), message=f"[algo] {tag}",
                        payload=payload)
        fill_price = await self._await_fill(order_id)
        return {"ok": True, "orderId": order_id, "fillPrice": fill_price,
                "status": res.get("orderStatus")}

    async def _await_fill(self, order_id: str, timeout: float = 10.0) -> Optional[float]:
        """Poll the order book briefly for an average traded price."""
        if not order_id:
            return None
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                book = await get_dhan_client().get_order_book()
            except DhanAPIError:
                book = []
            for o in book or []:
                if str(o.get("orderId")) == order_id:
                    status = str(o.get("orderStatus") or "").upper()
                    avg = o.get("averageTradedPrice") or o.get("averagePrice")
                    if status in ("TRADED", "PART_TRADED") and avg:
                        return float(avg)
                    if status in ("REJECTED", "CANCELLED", "EXPIRED"):
                        return None
            await asyncio.sleep(0.4)
        return None

    async def _place_protective_sl(self, pos: Position) -> None:
        """Park a WIDE real SL at Dhan so a server crash can't leave us naked."""
        delta = get_settings().algo_paper_delta
        dist_spot = abs(pos.entry_spot - pos.initial_stop_spot) * self.cfg.protective_sl_mult
        trigger_premium = pos.entry_premium - dist_spot * delta
        if trigger_premium <= 0.05:
            trigger_premium = 0.05
        res = await self._send_order(
            side="SELL", security_id=pos.security_id, qty=pos.quantity,
            segment=self._segment_for(pos.index_key), order_type="STOP_LOSS_MARKET",
            trigger=round(trigger_premium, 1),
            correlation_id=f"ORB-SL-{pos.index_key}-{uuid.uuid4().hex[:6]}", tag="protective_sl",
        )
        if res.get("ok"):
            pos.protective_order_id = res.get("orderId")
            self._log(
                f"{pos.index_key}: disaster stop parked at Dhan @ premium {trigger_premium:.1f} "
                f"({'simulated' if res.get('dryRun') else 'LIVE'})", level="info")
        else:
            self._log(f"{pos.index_key}: protective SL FAILED - {res.get('error')}", level="error")

    # ----------------------------------------------------------------- exit
    async def _exit_position(self, pos: Position, reason: str) -> None:
        self._busy = True
        try:
            if pos.protective_order_id and not pos.dry_run:
                try:
                    await get_dhan_client().cancel_order(str(pos.protective_order_id))
                    self._log(f"{pos.index_key}: disaster stop cancelled", level="info")
                except Exception as exc:  # noqa: BLE001 - best effort
                    self._log(
                        f"{pos.index_key}: could not cancel disaster stop "
                        f"({pos.protective_order_id}) - check Dhan: {exc}", level="error")
            price = pos.premium or pos.entry_premium
            res = await self._send_order(
                side="SELL", security_id=pos.security_id, qty=pos.quantity,
                segment=self._segment_for(pos.index_key), order_type="MARKET",
                correlation_id=f"ORB-EXIT-{pos.index_key}-{uuid.uuid4().hex[:6]}", tag="exit",
            )
            exit_premium = res.get("fillPrice") or price
            self._finalise_position(pos, exit_premium, reason, order_ok=res.get("ok", False))
        finally:
            self._busy = False

    def _finalise_position(
        self, pos: Position, exit_premium: float, reason: str, order_ok: bool = True
    ) -> None:
        gross_premium_pnl = (exit_premium - pos.entry_premium) * pos.quantity
        trade = {
            "date": self._day, "index": pos.index_key, "direction": pos.option_type,
            "strike": pos.strike, "expiry": pos.expiry, "lots": pos.lots,
            "quantity": pos.quantity,
            "entryTime": pos.entry_time, "exitTime": now_ist().strftime("%H:%M:%S"),
            "entrySpot": round(pos.entry_spot, 2), "exitSpot": round(pos.spot or pos.entry_spot, 2),
            "entryPremium": round(pos.entry_premium, 2), "exitPremium": round(exit_premium, 2),
            "targetSpot": pos.target_spot, "stopSpot": pos.stop_spot,
            "orRange": round(pos.or_range, 2),
            "points": round(pos.pnl_points, 2), "rMultiple": round(pos.r_multiple, 2),
            "mfe": round(pos.mfe, 2), "mae": round(pos.mae, 2),
            "premiumPnl": round(gross_premium_pnl, 2),
            "exitReason": reason, "dryRun": pos.dry_run,
        }
        self._today_trades.append(trade)
        store.append_trade(trade)
        if pos.dry_run:
            self._paper_balance = getattr(self, "_paper_balance", 100000.0) + gross_premium_pnl
        level = "success" if exit_premium >= pos.entry_premium else "warn"
        self._log(
            f"EXIT {pos.index_key} {pos.option_type} {int(pos.strike)} @ {exit_premium:.2f} "
            f"({reason})  spot {pos.points:+,.1f} pts  R {pos.r_multiple:+.2f}  "
            f"premium P&L Rs{gross_premium_pnl:,.0f}"
            + ("" if order_ok else "  [order may have failed - CHECK]"),
            level=level, trade=trade,
        )
        leg = self._legs.get(pos.index_key)
        if leg:
            leg.status = "closed"
        self._position = None
        self._persist()

    # --------------------------------------------------------------- status
    def _phase_label(self) -> str:
        return {
            "off": "OFF - algo is not armed",
            "preopen": "Pre-open - waiting for 09:15",
            "building_or": "Building opening range (09:15-09:25)",
            "watching": "Watching both indices for a breakout",
            "in_trade": "In a trade - managing stop & target",
            "day_done": "Done for today (trade taken or no breakout)",
            "closed": "Market closed",
        }.get(self.phase, self.phase)

    def _next_actions(self) -> List[str]:
        out: List[str] = []
        now = now_ist()
        minute = now.hour * 60 + now.minute
        if not self.enabled:
            out.append("Flip the switch ON to arm the algo.")
            return out
        if self._position is not None:
            p = self._position
            out.append(f"Watching spot against target {p.target_spot:,.1f} and stop {p.stop_spot:,.1f}.")
            if not p.breakeven_done and self.cfg.breakeven_at > 0:
                be = p.entry_spot + (self.cfg.breakeven_at * p.or_range
                                     * (1 if p.option_type == "CALL" else -1))
                out.append(f"Stop moves to break-even when spot touches {be:,.1f}.")
            out.append(f"Forced square-off at {self.cfg.exit_time} unless target/stop hits first.")
            return out
        if minute < 9 * 60 + 15:
            out.append("Waiting for the 09:15 open.")
        elif minute < 9 * 60 + 26:
            out.append("Will lock the opening range (09:15-09:25) and pre-pick contracts.")
        watching = [k for k, l in self._legs.items() if l.status == "watching"]
        if watching:
            out.append(f"Watching {', '.join(watching)} for the first break of the range.")
            out.append("First index to break takes the trade; the other is locked out for the day.")
        if minute >= _to_min(self.cfg.entry_cutoff, 15 * 60) and not self._today_trades:
            out.append(f"No fresh entries after {self.cfg.entry_cutoff}. Trade window closed.")
        if self._today_trades:
            out.append("Today's trade is done - waiting for the next session.")
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
            "dryRun": runtime.is_dry_run(),
            "serverTime": now.strftime("%Y-%m-%d %H:%M:%S"),
            "serverTimeShort": now.strftime("%H:%M:%S"),
            "day": self._day,
            "marketOpen": (now.weekday() < 5
                           and MARKET_OPEN_MIN <= now.hour * 60 + now.minute < MARKET_CLOSE_MIN),
            "config": asdict(self.cfg),
            "legs": legs,
            "position": self._position_dict(),
            "nextActions": self._next_actions(),
            "todayTrades": self._today_trades,
            "stats": self._stats(),
            "feed": feed.status(),
            "recovered": self._recovered,
            "lastError": self._last_error,
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
        }

    def _stats(self) -> Dict[str, Any]:
        closed = self._today_trades
        wins = [t for t in closed if t.get("premiumPnl", 0) > 0]
        week = store.recent_trades(days=7)
        return {
            "todayTrades": len(closed),
            "todayPnl": round(sum(t.get("premiumPnl", 0) for t in closed), 0),
            "todayR": round(sum(t.get("rMultiple", 0) for t in closed), 2),
            "todayWins": len(wins),
            "weekTrades": len(week),
            "weekPnl": round(sum(t.get("premiumPnl", 0) for t in week), 0),
            "weekR": round(sum(t.get("rMultiple", 0) for t in week), 2),
            "allTimePnl": round(sum(t.get("premiumPnl", 0) for t in store.read_trades(limit=5000)), 0),
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


algo = AlgoEngine()
