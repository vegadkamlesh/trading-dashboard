"""Market data endpoints — served from the warm in-memory cache (instant)."""
from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Query

from ..instruments import INDEX_REGISTRY, list_indices
from ..optionchain import engine
from ..security import limit, require_session

router = APIRouter(prefix="/api/market", tags=["market"])


@router.get("/indices")
async def indices(_session: str = Depends(require_session)) -> List[Dict[str, Any]]:
    return list_indices()


@router.get("/expiries")
async def expiries(
    index: str = Query(..., description="Index key e.g. NIFTY"),
    _session: str = Depends(require_session),
):
    key = index.upper()
    if key not in INDEX_REGISTRY:
        raise HTTPException(status_code=404, detail="Unknown index")
    # Triggers a refresh if we have nothing cached yet; otherwise returns cache.
    if not engine.expiry_list(key):
        await engine._fetch_one(key)  # noqa: SLF001 (intentional one-off warm)
    return {"index": key, "expiries": engine.expiry_list(key)}


@router.post("/expiry/select")
async def select_expiry(
    body: Dict[str, str],
    _session: str = Depends(require_session),
):
    key = (body.get("index") or "").upper()
    expiry = body.get("expiry") or ""
    if key not in INDEX_REGISTRY or not expiry:
        raise HTTPException(status_code=400, detail="index and expiry required")
    engine.set_expiry(key, expiry)
    return {"ok": True, "index": key, "expiry": expiry}


@router.get("/optionchain")
async def optionchain(
    index: str = Query(..., description="Index key e.g. NIFTY"),
    _session: str = Depends(require_session),
):
    key = index.upper()
    if key not in INDEX_REGISTRY:
        raise HTTPException(status_code=404, detail="Unknown index")
    # Light throttle: this just reads memory, but keep browsers polite.
    limit(f"oc_read:{key}", max_calls=10, window_seconds=1.0, message="Slow down")
    snap = engine.snapshot(key)
    if snap is None:
        # Not warmed yet (index not in poll list) — fetch once on demand.
        await engine._fetch_one(key)  # noqa: SLF001
        snap = engine.snapshot(key)
    if snap is None:
        raise HTTPException(status_code=503, detail="Option chain warming up, retry in a moment")
    return snap
