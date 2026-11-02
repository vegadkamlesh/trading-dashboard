"""Send a clearly labelled, account-wide AWS cost and EC2 activity report."""

import json
import logging
import os
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

import boto3
from botocore.exceptions import ClientError

from alerts import send_message

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
INDIA = ZoneInfo("Asia/Kolkata")


def observed_ec2_periods(cloudwatch, instance_id: str, now: datetime) -> int:
    now_utc = now.astimezone(timezone.utc)
    start = datetime.combine(now.astimezone(INDIA).date(), time.min, tzinfo=INDIA)
    result = cloudwatch.get_metric_statistics(
        Namespace="AWS/EC2",
        MetricName="CPUUtilization",
        Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
        StartTime=start.astimezone(timezone.utc),
        EndTime=now_utc,
        Period=300,
        Statistics=["SampleCount"],
    )
    timestamps = {
        point["Timestamp"]
        for point in result["Datapoints"]
        if point.get("SampleCount", 0) > 0
    }
    return len(timestamps)


def reported_costs(cost_explorer, now: datetime) -> tuple[Decimal | None, Decimal | None]:
    today = now.astimezone(timezone.utc).date()
    month_start = today.replace(day=1)
    try:
        response = cost_explorer.get_cost_and_usage(
            TimePeriod={
                "Start": month_start.isoformat(),
                "End": (today + timedelta(days=1)).isoformat(),
            },
            Granularity="DAILY",
            Metrics=["UnblendedCost"],
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "DataUnavailableException":
            raise
        logger.warning("AWS Cost Explorer data is not available yet")
        return None, None
    if response.get("NextPageToken"):
        raise RuntimeError("Cost Explorer returned an incomplete paginated response")

    daily: dict[str, Decimal] = {}
    for item in response["ResultsByTime"]:
        day = item["TimePeriod"]["Start"]
        metric = item["Total"].get("UnblendedCost")
        if metric is None:
            continue
        if metric["Unit"] != "USD":
            raise RuntimeError("Cost Explorer returned an unexpected currency")
        try:
            amount = Decimal(metric["Amount"])
        except (InvalidOperation, ValueError) as exc:
            raise RuntimeError("Cost Explorer returned an invalid amount") from exc
        if not amount.is_finite() or day in daily:
            raise RuntimeError("Cost Explorer returned duplicate or invalid daily data")
        daily[day] = amount

    expected_days = {
        (month_start + timedelta(days=offset)).isoformat()
        for offset in range((today - month_start).days + 1)
    }
    month = sum((daily[day] for day in expected_days), Decimal("0")) if expected_days <= daily.keys() else None
    return daily.get(today.isoformat()), month


def report_text(
    now: datetime,
    observed_periods: int,
    today_cost: Decimal | None,
    month_cost: Decimal | None,
    *,
    test: bool = False,
) -> str:
    utc_day = now.astimezone(timezone.utc).date().isoformat()
    if observed_periods:
        activity = (
            f"Current EC2 instance activity today (IST): about {observed_periods * 5} minutes "
            f"across {observed_periods} observed 5-minute CPU metric periods. "
            "Approximate; gaps or partial periods affect this."
        )
    else:
        activity = "Current EC2 instance activity today (IST): unknown; no CPU metric periods are available yet."
    daily = f"USD {today_cost:.4f}" if today_cost is not None else "not yet available"
    monthly = f"USD {month_cost:.4f}" if month_cost is not None else "not yet available"
    heading = "AWS daily usage report - TEST" if test else "AWS daily usage report"
    return (
        f"{heading}\n{activity}\n"
        f"AWS account unblended cost, UTC day {utc_day}: {daily}\n"
        f"AWS account month-to-date (UTC): {monthly}\n"
        "AWS billing data can lag 24+ hours and is estimated; missing or zero "
        "is not proof of no charges. UTC days start at 05:30 IST. "
        "This is the whole AWS account, not just EC2; credits, tax and final bills may differ. "
        "IP and storage can cost money while the server is stopped. "
        "Cost Explorer API: about USD 0.01 per request."
    )


def handler(event, _context):
    now = datetime.now(timezone.utc)
    instance_id = os.environ["INSTANCE_ID"]
    periods = observed_ec2_periods(boto3.client("cloudwatch"), instance_id, now)
    today_cost, month_cost = reported_costs(boto3.client("ce", region_name="us-east-1"), now)
    text = report_text(now, periods, today_cost, month_cost, test=event.get("test") is True)
    parameter = boto3.client("ssm").get_parameter(
        Name=os.environ["TELEGRAM_PARAMETER"], WithDecryption=True
    )
    credentials = json.loads(parameter["Parameter"]["Value"])
    send_message(credentials["token"], credentials["chat_id"], text)
    logger.info("Daily AWS usage report delivered to Telegram")
