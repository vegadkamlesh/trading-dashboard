"""Runtime settings + info endpoints.

Lets the user flip the two safety switches from the UI without editing .env or
restarting:
  * dryRun      -> true = simulate, false = send REAL orders
  * fastOrders  -> true = skip order PIN (1-click scalping), false = require PIN

Also exposes info used by the in-app README (log folder, retention, etc.).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from contextlib import AsyncExitStack

from .. import runtime
from ..algo import algo, engines, AlgoEngine
from ..accounts import account_scope
from ..config import get_settings
from ..logsetup import _log_dir
from ..security import require_csrf, require_session
from ..telegram_notify import notifier

router = APIRouter(prefix="/api/settings", tags=["settings"])


class ToggleBody(BaseModel):
    value: bool


@router.get("")
async def get_state(_session: str = Depends(require_session)):
    s = get_settings()
    return {
        "ok": True,
        **runtime.state(),
        "host": s.app_host,
        "port": s.app_port,
        "logDir": str(_log_dir()),
        "logRetentionDays": s.log_retention_days,
        "logLevel": s.log_level,
        "telegram": notifier.status(),
    }


@router.get("/telegram")
async def telegram_status(_session: str = Depends(require_session)):
    return {"ok": True, **notifier.status()}


@router.post("/telegram/test")
async def telegram_test(
    _session: str = Depends(require_session),
    _csrf: None = Depends(require_csrf),
):
    from ..security import limit

    limit("telegram-test", 3, 60, "Wait before sending another Telegram test")
    if not notifier.status()["running"]:
        raise HTTPException(409, "Telegram is disabled or stopped. Configure backend/.env and restart.")
    if not notifier.publish("test"):
        raise HTTPException(503, "Telegram notification queue unavailable. Check delivery status.")
    return {"ok": True, "delivery": "queued"}


@router.post("/dry-run")
async def set_dry_run(
    body: ToggleBody,
    _session: str = Depends(require_session),
    _csrf: None = Depends(require_csrf),
):
    targets = [(None, algo)] if isinstance(algo, AlgoEngine) else list(engines.items.items())
    async with engines.selection_lock, AsyncExitStack() as stack:
        for _, engine in targets:
            await stack.enter_async_context(engine._execution_lock)
        if body.value != runtime.is_dry_run():
            for account, engine in targets:
                with account_scope(account or "master"):
                    if engine._position is not None:
                        await engine._recover_historical_flat(engine._position)
                    if engine._position is not None:
                        detail = engine._position.execution_error
                        raise HTTPException(409, f"Mode unchanged: {account or 'master'} algo position/order is still tracked. "
                                            "Check Algo / Dhan and close or reconcile it first."
                                            + (f" Broker detail: {detail}" if detail else ""))
            for account, engine in targets:
                with account_scope(account or "master"):
                    engine.disarm("mode change - explicitly ARM again after checking settings")
            engines.entry_accounts = ()
        value = runtime.set_dry_run(body.value)
    return {"ok": True, "dryRun": value}


@router.post("/fast-orders")
async def set_fast_orders(
    body: ToggleBody,
    _session: str = Depends(require_session),
    _csrf: None = Depends(require_csrf),
):
    value = runtime.set_fast_orders(body.value)
    return {"ok": True, "fastOrders": value}
