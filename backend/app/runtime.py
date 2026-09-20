"""Runtime toggles that can be flipped from the UI without a restart.

The .env values are the *startup defaults*. From the UI, the user can switch:
  * dry_run      (true = simulate orders, false = send real orders)
  * fast_orders  (true = skip order PIN, false = require PIN)

These live in memory only and reset to the .env defaults on restart. That's
intentional: a restart always returns to the safe configured state.
"""
from __future__ import annotations

from .config import get_settings

_dry_run: bool | None = None
_fast_orders: bool | None = None


def _ensure_init() -> None:
    global _dry_run, _fast_orders
    if _dry_run is None or _fast_orders is None:
        s = get_settings()
        if _dry_run is None:
            _dry_run = s.app_dry_run
        if _fast_orders is None:
            _fast_orders = s.app_fast_orders


def is_dry_run() -> bool:
    _ensure_init()
    return bool(_dry_run)


def is_fast_orders() -> bool:
    _ensure_init()
    return bool(_fast_orders)


def set_dry_run(value: bool) -> bool:
    global _dry_run
    _ensure_init()
    _dry_run = bool(value)
    return _dry_run


def set_fast_orders(value: bool) -> bool:
    global _fast_orders
    _ensure_init()
    _fast_orders = bool(value)
    return _fast_orders


def state() -> dict:
    return {
        "dryRun": is_dry_run(),
        "fastOrders": is_fast_orders(),
    }
