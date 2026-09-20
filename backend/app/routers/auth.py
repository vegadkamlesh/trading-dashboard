"""Auth endpoints.

The Dhan token is already in the backend .env — "login" here only establishes
an app session cookie so the browser can call protected endpoints. We validate
the Dhan token by hitting /profile.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, Response

from .. import marketdata, runtime
from ..config import get_settings
from ..dhan_client import DhanAPIError, get_dhan_client
from ..security import CSRF_HEADER, SESSION_COOKIE, create_session, require_session

logger = logging.getLogger("dhan.auth")
router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login")
async def login(response: Response):
    """Validate the configured Dhan token and start an app session."""
    s = get_settings()
    try:
        profile = await get_dhan_client().get_profile()
    except DhanAPIError as exc:
        return {
            "ok": False,
            "error": exc.to_dict(),
        }

    # Warm the daily-OHLC cache in the background (non-blocking) so predictions
    # and seasonality are instant on first open — the heavy historical pull
    # happens once, here, instead of on every signal refresh.
    try:
        asyncio.create_task(marketdata.warm_up())
    except RuntimeError:  # pragma: no cover - already-running loop edge case
        logger.warning("could not schedule marketdata warm-up")

    token = create_session()
    secure = not s.app_host.startswith("127.") and not s.app_host.startswith("localhost")
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        httponly=True,
        samesite="strict",
        secure=secure,
        max_age=12 * 3600,
        path="/",
    )
    # CSRF token == session token; frontend reads it from this response body.
    return {
        "ok": True,
        "csrfToken": token,
        "profile": {
            "clientId": profile.get("dhanClientId"),
            "name": profile.get("dhanClientName"),
            "activeSegment": profile.get("activeSegment"),
            "ddpi": profile.get("ddpi"),
            "dataPlan": profile.get("dataPlan"),
            "tokenValidity": profile.get("tokenValidity"),
        },
        "dryRun": runtime.is_dry_run(),
        "fastOrders": runtime.is_fast_orders(),
    }


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
async def me(session: str = Depends(require_session)):
    # Return the CSRF token (== session token) so the frontend can always re-arm
    # it after a page reload / new tab. Without this, a restored session had no
    # token and every POST failed with 403.
    return {
        "ok": True,
        "csrfToken": session,
        "dryRun": runtime.is_dry_run(),
        "fastOrders": runtime.is_fast_orders(),
    }
