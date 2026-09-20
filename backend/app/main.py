"""FastAPI application entry point."""
from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import audit, runtime
from .config import get_settings
from .dhan_client import DhanAPIError, close_dhan_client
from .logsetup import daily_cleanup_loop, setup_logging
from .optionchain import engine
from .routers import (
    account,
    audit as audit_router,
    auth,
    intel,
    market,
    orders,
    settings as settings_router,
)

# Configure file logging FIRST so startup is captured. Falls back to console-only
# if the log directory can't be created (e.g. read-only disk).
try:
    setup_logging()
except Exception as _exc:  # pragma: no cover - defensive
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("dhan.app").warning("File logging disabled: %s", _exc)

logger = logging.getLogger("dhan.app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    if not s.is_configured():
        logger.error(
            "DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN are not set. "
            "Create backend/.env (see backend/env.example.txt). "
            "Auth and order endpoints will fail until configured."
        )
    audit.init_db()
    pruned = audit.prune_old_logs()
    if pruned:
        logger.info("Pruned %d audit row(s) older than %d days", pruned, s.log_retention_days)

    # Background thread: delete log files older than the retention window daily.
    threading.Thread(target=daily_cleanup_loop, daemon=True, name="log-cleanup").start()

    logger.info(
        "SAFETY: dryRun=%s (true=simulate, false=REAL orders) | fastOrders=%s "
        "(true=no PIN, 1-click)",
        runtime.is_dry_run(),
        runtime.is_fast_orders(),
    )
    if s.is_configured():
        await engine.start()
        logger.info("Option chain poller started for: %s", s.oc_polled_indices)
    try:
        yield
    finally:
        await engine.stop()
        await close_dhan_client()
        logger.info("Shutdown complete")


app = FastAPI(title="Dhan Options Dashboard", version="1.0.0", lifespan=lifespan)

settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.app_allowed_origins,  # explicit only, never "*"
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-CSRF-Token"],
)


@app.exception_handler(DhanAPIError)
async def dhan_error_handler(_request, exc: DhanAPIError):
    return JSONResponse(status_code=502, content={"ok": False, "error": exc.to_dict()})


@app.get("/api/health")
async def health():
    return {"ok": True, **runtime.state()}


app.include_router(auth.router)
app.include_router(market.router)
app.include_router(orders.router)
app.include_router(account.router)
app.include_router(audit_router.router)
app.include_router(intel.router)
app.include_router(settings_router.router)


@app.middleware("http")
async def access_log(request, call_next):
    """Log every API call to access-*.log (method, path, status, duration)."""
    import time as _t

    start = _t.perf_counter()
    response = await call_next(request)
    dur_ms = (_t.perf_counter() - start) * 1000
    if request.url.path.startswith("/api"):
        logging.getLogger("dhan.access").info(
            "%s %s -> %d (%.1fms)",
            request.method,
            request.url.path,
            response.status_code,
            dur_ms,
        )
    return response


def run() -> None:
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=False,
    )


if __name__ == "__main__":
    run()

