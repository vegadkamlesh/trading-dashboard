"""Start the dashboard server on weekday mornings unless NSE is closed for a holiday."""

import json
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import boto3

from alerts import send_message
from market_holidays import holiday_list_available, is_trading_holiday

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def decide(now: datetime) -> str:
    """Return "skip-weekend", "skip-holiday", "start" or "start-unverified"."""
    day = now.date()
    if now.weekday() >= 5:
        return "skip-weekend"
    if is_trading_holiday(day):
        return "skip-holiday"
    return "start" if holiday_list_available(day) else "start-unverified"


def notify(text: str) -> None:
    # Telegram is informational only; a delivery failure must never block the start.
    try:
        parameter = boto3.client("ssm").get_parameter(
            Name=os.environ["TELEGRAM_PARAMETER"], WithDecryption=True
        )
        credentials = json.loads(parameter["Parameter"]["Value"])
        send_message(credentials["token"], credentials["chat_id"], text)
    except Exception:  # noqa: BLE001
        logger.exception("Start-gate Telegram notification failed")


def handler(event, _context):
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    decision = decide(now)
    logger.info("Start gate decision for %s: %s", now.date().isoformat(), decision)
    if isinstance(event, dict) and event.get("dry_run") is True:
        return {"decision": decision, "dry_run": True}
    if decision.startswith("skip"):
        if decision == "skip-holiday":
            notify(f"Trading dashboard: {now:%d-%b-%Y} is an NSE trading holiday. Server start skipped.")
        return {"decision": decision}
    boto3.client("ec2").start_instances(InstanceIds=[os.environ["INSTANCE_ID"]])
    if decision == "start-unverified":
        notify(
            f"Trading dashboard: NSE holiday list for {now.year} is missing. "
            "Server started; add the year to deployment/aws/market_holidays.py."
        )
    return {"decision": decision}
