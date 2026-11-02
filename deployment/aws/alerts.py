"""Send fixed, non-sensitive AWS operational notifications to Telegram."""

import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import boto3


def notification_text(subject: str, message: str) -> str:
    if subject == "TEST: trading-dashboard-alerts":
        return "AWS TEST: operational notifications are working."
    lowered = subject.lower()
    if "budget" in lowered:
        return "AWS budget threshold reached. Review AWS Billing; alerts do not cap charges."
    if subject.startswith("ALARM:"):
        if "availability" in lowered:
            return "AWS ALERT: the dashboard is unavailable or not in its expected scheduled state."
        if "memory" in lowered:
            return "AWS ALERT: dashboard server memory is high. Check CloudWatch before trading."
        if "disk" in lowered:
            return "AWS ALERT: dashboard storage is nearly full. Check CloudWatch and disk usage."
        if "cpu-credit" in lowered:
            return "AWS ALERT: server CPU burst credits are low. Check latency and possible extra charges."
        if "cost-report" in lowered:
            return "AWS ALERT: the daily usage report failed. Check its CloudWatch logs."
        if "scheduler" in lowered:
            return "AWS ALERT: a scheduled server start or stop failed. Check the scheduler queue."
        if "instance-status" in lowered:
            return "AWS ALERT: EC2 reported an instance or underlying hardware status failure."
        return "AWS ALERT: review the dashboard CloudWatch alarms."
    if subject.startswith("OK:"):
        return "AWS monitoring alarm recovered. Review CloudWatch for details."
    try:
        payload = json.loads(message)
    except (TypeError, ValueError):
        payload = None
    if isinstance(payload, dict) and payload.get("detail-type") == "Backup Job State Change":
        return "AWS ALERT: a dashboard backup failed or was aborted. Review AWS Backup."
    return "AWS operational notification. Review CloudWatch, Scheduler, Backup and Billing."


def send_message(token: str, chat_id: str, text: str) -> None:
    request = Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=8) as response:
            result = json.loads(response.read(65536))
    except HTTPError as exc:
        raise RuntimeError(f"Telegram delivery failed with HTTP {exc.code}") from None
    except (URLError, TimeoutError, ValueError):
        raise RuntimeError("Telegram delivery failed or returned invalid JSON") from None
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise RuntimeError("Telegram did not confirm delivery")


def handler(event, _context):
    parameter = boto3.client("ssm").get_parameter(
        Name=os.environ["TELEGRAM_PARAMETER"], WithDecryption=True
    )
    credentials = json.loads(parameter["Parameter"]["Value"])
    for record in event["Records"]:
        sns = record["Sns"]
        send_message(
            credentials["token"],
            credentials["chat_id"],
            notification_text(sns.get("Subject") or "", sns.get("Message") or ""),
        )
    print("AWS notification delivered to Telegram")
