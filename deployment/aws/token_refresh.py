"""Generate fresh 24-hour Dhan access tokens every trading morning using Dhan TOTP login.

Reads the PIN/TOTP secrets from an encrypted parameter only this function may read, updates
just the token keys in the dashboard's Dhan parameter and reports the result to Telegram.
The instance copies the new tokens into its .env before the service starts (sync_dhan_env.py).
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import struct
import time
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import boto3

from alerts import send_message
from market_holidays import is_trading_holiday

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

IST = ZoneInfo("Asia/Kolkata")
AUTH_URL = "https://auth.dhan.co/app/generateAccessToken"
PROFILE_URL = "https://api.dhan.co/v2/profile"
# account -> (client-id key, token key) in the dashboard's Dhan parameter
ACCOUNTS = {
    "master": ("DHAN_CLIENT_ID", "DHAN_ACCESS_TOKEN"),
    "child": ("DHAN_CHILD_CLIENT_ID", "DHAN_CHILD_ACCESS_TOKEN"),
}


def totp(secret: str, at: float, step: int = 30, digits: int = 6) -> str:
    """RFC 6238 TOTP (HMAC-SHA1) for a base32 secret."""
    cleaned = secret.replace(" ", "").upper()
    key = base64.b32decode(cleaned + "=" * (-len(cleaned) % 8))
    digest = hmac.new(key, struct.pack(">Q", int(at) // step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 10 ** digits
    return f"{code:0{digits}d}"


def _json_request(request: Request) -> dict:
    try:
        with urlopen(request, timeout=15) as response:
            body = json.loads(response.read(65536) or b"{}")
    except HTTPError as exc:
        try:
            detail = json.loads(exc.read(4096) or b"{}")
        except ValueError:
            detail = {}
        message = detail.get("errorMessage") or detail.get("message") or detail.get("status") or ""
        raise RuntimeError(f"HTTP {exc.code} {str(message)[:120]}".strip()) from None
    except (URLError, TimeoutError, ValueError) as exc:
        raise RuntimeError(f"network/JSON error: {type(exc).__name__}") from None
    if not isinstance(body, dict):
        raise RuntimeError("unexpected response shape")
    return body


def generate_token(client_id: str, pin: str, secret: str, now=time.time, sleep=time.sleep) -> dict:
    """Return Dhan's response with accessToken; retries once in the next TOTP window."""
    last_error = None
    for attempt in range(2):
        if attempt:
            sleep(31 - now() % 30)
        query = urlencode({"dhanClientId": client_id, "pin": pin, "totp": totp(secret, now())})
        try:
            body = _json_request(Request(f"{AUTH_URL}?{query}", method="POST", data=b""))
        except RuntimeError as exc:
            last_error = exc
            continue
        if body.get("accessToken") and str(body.get("dhanClientId", client_id)) == client_id:
            return body
        last_error = RuntimeError(str(body.get("message") or body.get("errorMessage") or "no accessToken")[:120])
    raise RuntimeError(f"token generation failed: {last_error}")


def validate_token(client_id: str, token: str) -> None:
    body = _json_request(Request(PROFILE_URL, headers={"access-token": token, "dhanClientId": client_id}))
    if str(body.get("dhanClientId", "")) != client_id:
        raise RuntimeError("profile check returned a different client")


def notify(text: str) -> None:
    try:
        parameter = boto3.client("ssm").get_parameter(Name=os.environ["TELEGRAM_PARAMETER"], WithDecryption=True)
        credentials = json.loads(parameter["Parameter"]["Value"])
        send_message(credentials["token"], credentials["chat_id"], text)
    except Exception:  # noqa: BLE001
        logger.exception("Token-refresh Telegram notification failed")


def refresh(totp_config: dict, current: dict, accounts=None, generate=generate_token,
            validate=validate_token) -> tuple[dict, dict]:
    """Return (updated Dhan parameter values, per-account result text)."""
    updated = dict(current)
    results = {}
    for account in accounts or ACCOUNTS:
        entry = totp_config.get(account)
        id_key, token_key = ACCOUNTS[account]
        if not entry:
            continue
        client_id = str(entry["client_id"])
        try:
            body = generate(client_id, str(entry["pin"]), str(entry["totp_secret"]))
            validate(client_id, body["accessToken"])
        except Exception as exc:  # noqa: BLE001
            results[account] = f"FAILED ({exc})"
            continue
        updated[id_key] = client_id
        updated[token_key] = body["accessToken"]
        results[account] = f"OK, valid till {body.get('expiryTime', '?')}"
    return updated, results


def handler(event, _context):
    event = event if isinstance(event, dict) else {}
    now = datetime.now(IST)
    if not event.get("force") and (now.weekday() >= 5 or is_trading_holiday(now.date())):
        logger.info("Skipping token refresh on a non-trading day %s", now.date())
        return {"skipped": True}
    ssm = boto3.client("ssm")
    dhan_name, totp_name = os.environ["DHAN_PARAMETER"], os.environ["TOTP_PARAMETER"]
    totp_config = json.loads(ssm.get_parameter(Name=totp_name, WithDecryption=True)["Parameter"]["Value"])
    current = json.loads(ssm.get_parameter(Name=dhan_name, WithDecryption=True)["Parameter"]["Value"])
    accounts = event.get("accounts") or None
    updated, results = refresh(totp_config, current, accounts=accounts)
    if updated != current:
        ssm.put_parameter(Name=dhan_name, Type="SecureString", Overwrite=True,
                          Value=json.dumps(updated, separators=(",", ":")))
    ok = bool(results) and all(text.startswith("OK") for text in results.values())
    lines = [f"Dhan token refresh {now:%d-%b %H:%M}: {'OK' if ok else 'PROBLEM'}"]
    lines += [f"{name.title()}: {text}" for name, text in results.items()] or ["No TOTP accounts configured"]
    if not ok:
        lines.append("Old token stays in place. Fix TOTP/PIN or update the token manually before 09:15.")
    logger.info("Token refresh results: %s", results)
    notify("\n".join(lines))
    if not ok:
        failed = [name for name, text in results.items() if not text.startswith("OK")]
        raise RuntimeError("Dhan token refresh failed for: " + (", ".join(failed) or "no accounts"))
    return {"ok": True, "accounts": sorted(results)}
