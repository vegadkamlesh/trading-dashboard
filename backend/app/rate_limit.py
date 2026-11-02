"""Process-wide Dhan request budgets; writes are never automatically replayed."""
from __future__ import annotations

import asyncio
import time
from collections import deque


class RateGate:
    def __init__(self, gap: float, limits: tuple[tuple[int, float], ...] = ()) -> None:
        self.gap = gap
        self.limits = limits
        self._windows: list[tuple[int, float, deque[float]]] = [
            (count, window, deque()) for count, window in limits
        ]
        self._last: float | None = None
        self._blocked_until = 0.0
        self._lock = asyncio.Lock()

    def defer(self, seconds: float) -> None:
        self._blocked_until = max(self._blocked_until, time.monotonic() + seconds)

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                ready = max(self._blocked_until, (self._last + self.gap) if self._last is not None else now)
                for count, window, stamps in self._windows:
                    while stamps and stamps[0] <= now - window:
                        stamps.popleft()
                    if len(stamps) >= count:
                        ready = max(ready, stamps[-count] + window)
                if ready <= now:
                    self._last = now
                    for _, _, stamps in self._windows:
                        stamps.append(now)
                    return
                await asyncio.sleep(ready - now)


ORDER_GATE = RateGate(.12, ((250, 60), (1000, 3600), (7000, 86400)))
DATA_GATE = RateGate(.25, ((100000, 86400),))
QUOTE_GATE = RateGate(1.05)
READ_GATE = RateGate(.06)
CHAIN_GATE = RateGate(3.1)
_account_gates: dict[str, tuple[RateGate, RateGate]] = {}
_chain_gates: dict[tuple[str, str, str], RateGate] = {}


def chain_gate_for(client_id: str, underlying: str, expiry: str) -> RateGate:
    key = (client_id, underlying, expiry)
    if key not in _chain_gates:
        _chain_gates[key] = RateGate(3.1)
    return _chain_gates[key]


def gate_for(method: str, path: str, client_id: str = "") -> RateGate:
    if path.startswith("/marketfeed/"):
        return QUOTE_GATE
    if path.startswith("/optionchain"):
        return CHAIN_GATE
    if path.startswith("/charts/"):
        return DATA_GATE
    if client_id:
        if client_id not in _account_gates:
            _account_gates[client_id] = (
                RateGate(.12, ((250, 60), (1000, 3600), (7000, 86400))), RateGate(.06),
            )
        gates = _account_gates[client_id]
        return gates[0] if method != "GET" and (
            path.startswith("/orders") or path.startswith("/super/orders")
        ) else gates[1]
    if method != "GET" and (path.startswith("/orders") or path.startswith("/super/orders")):
        return ORDER_GATE
    return READ_GATE
