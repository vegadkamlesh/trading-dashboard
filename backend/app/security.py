"""Session auth, CSRF, in-process rate limiting, and order PIN verification.

Design notes
------------
* Sessions are signed tokens (itsdangerous) delivered as HttpOnly cookies.
  The browser never stores the token in JS-accessible storage.
* CSRF: every non-GET request must echo the token in the `X-CSRF-Token`
  header. It is compared in constant time against the session token.
* Rate limiting is a simple in-process sliding window. Good enough for a
  single local user; swap for Redis if you ever multi-instance it.
* Order PIN is compared with `hmac.compare_digest` (constant time).
"""
from __future__ import annotations

import hmac
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Optional

from fastapi import Cookie, Header, HTTPException, Request, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .config import get_settings
from .runtime import is_fast_orders

SESSION_COOKIE = "dhan_session"
CSRF_HEADER = "x-csrf-token"
SESSION_MAX_AGE = 60 * 60 * 12  # 12 hours


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().app_session_secret, salt="session")


def create_session() -> str:
    return _serializer().dumps({"auth": True})


def verify_session(token: Optional[str]) -> bool:
    if not token:
        return False
    try:
        data = _serializer().loads(token, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return False
    return bool(data.get("auth"))


def require_session(session: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE)) -> str:
    if not verify_session(session):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return session  # type: ignore[return-value]


def require_csrf(
    request: Request,
    session: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE),
    csrf: Optional[str] = Header(default=None, alias=CSRF_HEADER),
) -> None:
    """Enforce CSRF for any state-changing request."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    if not verify_session(session):
        raise HTTPException(status_code=401, detail="Not authenticated")
    # The CSRF token must equal the session token (both HttpOnly-derived).
    if not csrf or not hmac.compare_digest(csrf, session or ""):
        raise HTTPException(status_code=403, detail="CSRF validation failed")


def verify_order_pin(pin: Optional[str]) -> None:
    """Validate the order PIN.

    FAST ORDERS: when APP_FAST_ORDERS is enabled, the PIN is skipped entirely so
    a single click places the order (scalping). This is a deliberate safety
    trade-off — enable it only while actively scalping.
    """
    if is_fast_orders():
        return
    expected = get_settings().app_order_pin
    if not pin or not hmac.compare_digest(str(pin), str(expected)):
        raise HTTPException(status_code=403, detail="Invalid order confirmation PIN")


# ------------------- rate limiting -------------------
class SlidingWindowLimiter:
    def __init__(self) -> None:
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)

    def check(self, key: str, max_calls: int, window_seconds: float) -> bool:
        now = time.monotonic()
        dq = self._hits[key]
        while dq and now - dq[0] > window_seconds:
            dq.popleft()
        if len(dq) >= max_calls:
            return False
        dq.append(now)
        return True


limiter = SlidingWindowLimiter()


def limit(key: str, max_calls: int, window_seconds: float, message: str = "Too many requests") -> None:
    if not limiter.check(key, max_calls, window_seconds):
        raise HTTPException(status_code=429, detail=message)
