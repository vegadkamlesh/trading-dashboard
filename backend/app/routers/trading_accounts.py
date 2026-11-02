"""Explicit multi-account control; never fan out a destructive action implicitly."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
from copy import deepcopy
from contextlib import AsyncExitStack

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import audit, runtime
from ..accounts import (ACCOUNT_IDS, AccountId, account_credentials, account_name,
                        account_scope, select_accounts, selected_accounts)
from ..algo import engines, now_ist
from ..config import get_settings
from ..dhan_client import DhanAPIError, get_trading_client, is_mock_token
from ..security import require_csrf, require_session

logger = logging.getLogger("dhan.accounts")
router = APIRouter(prefix="/api/trading", tags=["trading-accounts"],
                   dependencies=[Depends(require_session)])


class Selection(BaseModel):
    accounts: list[AccountId]


@router.get("/accounts")
async def status():
    return {
        "dataSource": "master",
        "dataSourceMock": is_mock_token(get_settings().dhan_access_token),
        "selected": selected_accounts(),
        "accounts": [{
            "id": account, "name": account_name(account),
            "configured": bool(client_id and token),
            "mock": bool(client_id and token and is_mock_token(token)),
            "enabled": account in selected_accounts(),
            "algoEnabled": engines.items[account].enabled,
            "algoEntryEnabled": account in engines.entry_accounts and engines.items[account].enabled,
            "positionState": engines.items[account]._position.execution_state
            if engines.items[account]._position else None,
            "error": engines.items[account]._last_error,
        } for account, (client_id, token) in (
            (account, account_credentials(account)) for account in ACCOUNT_IDS
        )],
    }


@router.post("/selection", dependencies=[Depends(require_csrf)])
async def selection(body: Selection):
    async with engines.selection_lock, AsyncExitStack() as stack:
        for engine in engines.items.values():
            await stack.enter_async_context(engine._execution_lock)
        if any(engine._position is not None or engine.enabled for engine in engines.items.values()):
            raise HTTPException(409, "Disarm the algo and finish tracked positions before changing recipients. Existing exits remain available.")
        select_accounts(body.accounts)
        engines.entry_accounts = ()
        logger.info("selection_changed recipients=%s dry_run=%s", selected_accounts(), runtime.is_dry_run())
    return await status()


async def account_result(account: AccountId, action: str, callback):
    with account_scope(account):
        try:
            result = await callback()
            if isinstance(result, dict) and result.get("ok") is False:
                error = result.get("error") or "Action not accepted; inspect this account at Dhan."
                logger.error("account=%s action=%s reason=%s", account, action, error)
                return {"accountId": account, "ok": False, "error": error, "result": result}
            logger.info("account=%s action=%s result=completed dry_run=%s", account, action, runtime.is_dry_run())
            return {"accountId": account, "ok": True, "result": result}
        except HTTPException as exc:
            detail = exc.detail
            logger.error("account=%s action=%s http=%s reason=%s", account, action, exc.status_code, detail)
            return {"accountId": account, "ok": False, "error": detail}
        except DhanAPIError as exc:
            logger.error("account=%s action=%s code=%s reason=%s", account, action, exc.error_code, exc.error_message)
            return {"accountId": account, "ok": False, "error": exc.to_dict()}
        except (OSError, sqlite3.Error) as exc:
            logger.exception("account=%s action=%s persistence_failure", account, action)
            return {"accountId": account, "ok": False, "error": "Persistence failed; inspect broker before retrying."}
        except Exception:
            logger.exception("account=%s action=%s outcome=unknown", account, action)
            return {"accountId": account, "ok": False,
                    "error": "Unexpected failure; outcome may be unknown. Check this account at Dhan before any retry."}


@router.post("/preview", dependencies=[Depends(require_csrf)])
async def preview(body: dict):
    from .orders import OrderPreviewBody, preview as preview_one
    from pydantic import ValidationError
    try:
        request = OrderPreviewBody.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    recipients = selected_accounts()
    if not recipients:
        raise HTTPException(409, "Enable at least one trading account")
    results = await asyncio.gather(*(account_result(a, "preview",
        lambda: preview_one(request, "session", None)) for a in recipients))
    return {"results": results, "selected": recipients}


@router.post("/place", dependencies=[Depends(require_csrf)])
async def place(body: dict):
    from .orders import OrderPlaceBody, place as place_one
    from pydantic import ValidationError
    try:
        request = OrderPlaceBody.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    async with engines.selection_lock:
        recipients = selected_accounts()
        if not recipients:
            raise HTTPException(409, "Enable at least one trading account")
        expected = body.get("accountIds")
        if expected != list(recipients):
            raise HTTPException(409, "Trading recipients changed. Refresh the preview before confirming.")
        from ..security import verify_order_pin
        verify_order_pin(request.pin)
        # The same request ID cannot acquire new recipients after a retry/restart.
        intent = request.model_dump(mode="json", exclude={"pin"})
        fingerprint = hashlib.sha256(json.dumps(
            {"order": intent, "accounts": recipients, "dryRun": runtime.is_dry_run()}, sort_keys=True,
        ).encode()).hexdigest()
        key = f"batch:{request.requestId}"
        with account_scope("master"):
            try:
                claimed, previous = audit.claim_manual_request(key, fingerprint)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            except sqlite3.Error as exc:
                raise HTTPException(503, "Cannot persist account recipients. No orders sent.") from exc
        if not claimed:
            if previous:
                return previous
            raise HTTPException(409, "Batch outcome is pending/unknown. Inspect both accounts; no automatic replay.")
        logger.info("batch=%s action=manual_entry recipients=%s", request.requestId, recipients)
        results = await asyncio.gather(*(account_result(a, f"entry batch={request.requestId}",
            lambda: place_one(request, "session", None)) for a in recipients))
        response = {"ok": all(r["ok"] for r in results), "dryRun": runtime.is_dry_run(),
                    "results": results, "requestId": str(request.requestId)}
        with account_scope("master"):
            try:
                audit.finish_manual_request(key, response)
            except sqlite3.Error as exc:
                logger.exception("batch=%s receipt persistence failed after attempts", request.requestId)
                raise HTTPException(503, "Batch attempted but receipt could not be saved. Inspect both accounts at Dhan; do not resubmit.") from exc
        return response


class AlgoAction(BaseModel):
    action: str


@router.post("/algo", dependencies=[Depends(require_csrf)])
async def algo_action(body: AlgoAction):
    if body.action not in {"arm", "disarm", "exit"}:
        raise HTTPException(400, "Unknown algo action")
    async with engines.selection_lock:
        if body.action == "disarm":
            async with AsyncExitStack() as stack:
                for engine in engines.items.values():
                    await stack.enter_async_context(engine._execution_lock)
                engines.entry_accounts = ()
                for account, engine in engines.items.items():
                    with account_scope(account):
                        engine.disarm("operator - all accounts; holding management continues")
            return {"ok": True}
        recipients = selected_accounts() if body.action == "arm" else ACCOUNT_IDS
        if not recipients:
            raise HTTPException(409, "Enable a trading account first")
        if body.action == "arm":
            async with AsyncExitStack() as stack:
                for engine in engines.items.values():
                    await stack.enter_async_context(engine._execution_lock)
                if any(engine.enabled for engine in engines.items.values()):
                    raise HTTPException(409, "Algo already armed. Disarm before changing the shared run.")
                if any(engine._position for engine in engines.items.values()):
                    raise HTTPException(409, "A position is already tracked. Existing management continues; finish it before a new shared ARM.")
                results = []
                for account in recipients:
                    with account_scope(account):
                        engine = engines.items[account]
                        if not runtime.is_dry_run():
                            check = await engine._preflight_ip()
                            if not check["ok"]:
                                engine._last_error = check["reason"]
                                engine._log(f"ARM refused: {check['reason']}", level="error")
                                results.append({"accountId": account, "ok": False, "error": check["reason"]})
                                continue
                        engine._last_error = None
                        results.append({"accountId": account, "ok": True})
                ready = tuple(r["accountId"] for r in results if r["ok"])
                engines.entry_accounts = ()
                try:
                    for account in ACCOUNT_IDS:
                        with account_scope(account):
                            engine = engines.items[account]
                            if account != "master":
                                engine.cfg = deepcopy(engines.items["master"].cfg)
                            engine.enabled = account in ready or (account == "master" and bool(ready))
                            engine._log("Shared algo recipients ready=" + ",".join(ready))
                            engine._persist()
                except OSError as exc:
                    for engine in engines.items.values():
                        engine.enabled = False
                        engine._last_error = "Shared ARM failed to persist; all new entries disarmed."
                    logger.exception("Shared ARM persistence failed; no new entries armed")
                    raise HTTPException(503, "Cannot persist ARM state. Both accounts are disarmed; repair storage before retrying.") from exc
                engines.entry_accounts = ready
                return {"ok": len(ready) == len(recipients), "armed": bool(ready), "results": results}
        async def panic():
            result = await engines.current().panic_exit("EXIT BOTH")
            position = result.get("position")
            if position and position.get("executionError"):
                raise HTTPException(409, position["executionError"])
            return result
        results = await asyncio.gather(*(account_result(a, "algo_exit", panic) for a in recipients))
        return {"ok": all(r["ok"] for r in results), "results": results}


async def apply_startup_defaults() -> dict:
    """Apply .env account selection and optional auto-arm once, right after engines start."""
    s = get_settings()
    try:
        async with engines.selection_lock:
            select_accounts(s.trading_default_accounts)
    except HTTPException as exc:
        logger.error("startup_defaults selection failed: %s", exc.detail,
                     extra={"telegram_event": "auto_arm_failed" if s.algo_auto_arm else "error"})
        return {"ok": False, "error": exc.detail}
    logger.info("startup_defaults recipients=%s autoArm=%s", selected_accounts(), s.algo_auto_arm)
    if not s.algo_auto_arm:
        return {"ok": True, "armed": False}
    try:
        result = await algo_action(AlgoAction(action="arm"))
    except HTTPException as exc:
        logger.error("startup_auto_arm refused: %s", exc.detail, extra={"telegram_event": "auto_arm_failed"})
        return {"ok": False, "error": exc.detail}
    except Exception:
        logger.exception("startup_auto_arm failed", extra={"telegram_event": "auto_arm_failed"})
        return {"ok": False, "error": "unexpected"}
    mode = "DRY-RUN" if runtime.is_dry_run() else "LIVE"
    armed = ",".join(engines.entry_accounts) or "none"
    if result.get("ok"):
        logger.info("startup_auto_arm armed=%s", armed,
                    extra={"telegram_event": "auto_armed", "telegram_account": armed, "telegram_mode": mode})
    else:
        logger.error("startup_auto_arm partial armed=%s results=%s", armed, result.get("results"),
                     extra={"telegram_event": "auto_arm_failed", "telegram_account": armed, "telegram_mode": mode})
    return result


class ExitBoth(BaseModel):
    securityId: str
    exchangeSegment: str
    productType: str


@router.post("/exit-both", dependencies=[Depends(require_csrf)])
async def exit_both(body: ExitBoth):
    from .account import SquareOffBody, square_off
    async def exit_one():
        rows = await get_trading_client().get_positions(fresh=True)
        held = sum(int(row.get("netQty") or 0) for row in rows
                   if str(row.get("securityId")) == body.securityId
                   and row.get("exchangeSegment") == body.exchangeSegment
                   and row.get("productType") == body.productType)
        if not held:
            return {"status": "ALREADY_FLAT"}
        return await square_off(SquareOffBody(**body.model_dump(), quantity=abs(held),
            transactionType="SELL" if held > 0 else "BUY"), "session", None)
    # Exits deliberately include disabled accounts: turning off entries must not strand holdings.
    results = await asyncio.gather(*(account_result(a, "manual_exit_both", exit_one)
        for a in ACCOUNT_IDS if all(account_credentials(a))))
    return {"ok": all(r["ok"] for r in results), "results": results}
