"""Audit / EOD summary endpoints."""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Query

from ..audit import eod_summary
from ..security import require_session

router = APIRouter(prefix="/api/audit", tags=["audit"])


@router.get("/eod")
async def eod(
    hours: float = Query(24.0, gt=0, le=168),
    _session: str = Depends(require_session),
):
    return eod_summary(time.time() - hours * 3600)
