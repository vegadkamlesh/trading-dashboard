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

from .. import runtime
from ..config import get_settings
from ..logsetup import _log_dir
from ..security import require_csrf, require_session

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
    }


@router.post("/dry-run")
async def set_dry_run(
    body: ToggleBody,
    _session: str = Depends(require_session),
    _csrf: None = Depends(require_csrf),
):
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
