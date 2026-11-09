"""Check scheduled EC2 and local dashboard health independently of the host."""

import logging
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import boto3
from botocore.exceptions import ClientError

from market_holidays import is_trading_holiday

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def expected_state(now: datetime) -> str:
    if now.weekday() >= 5 or is_trading_holiday(now.date()):
        return "stopped"
    minute = now.hour * 60 + now.minute
    if 9 * 60 + 10 <= minute < 16 * 60:
        return "running"
    if 8 * 60 + 55 <= minute < 9 * 60 + 10 or 16 * 60 <= minute < 16 * 60 + 15:
        return "grace"
    return "stopped"


def dashboard_healthy(ssm, instance_id: str) -> bool:
    try:
        command = ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            TimeoutSeconds=30,
            Parameters={
                "commands": [
                    "set -e",
                    "systemctl is-active --quiet trading-dashboard.service",
                    "systemctl is-active --quiet nginx.service",
                    "systemctl is-active --quiet amazon-cloudwatch-agent.service",
                    "curl -fsS --max-time 3 http://127.0.0.1:8080/api/health > /dev/null",
                ],
                "executionTimeout": ["15"],
            },
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("InvalidInstanceId", "TargetNotConnected"):
            logger.warning("Scheduled host is not reachable by Systems Manager")
            return False
        raise
    command_id = command["Command"]["CommandId"]
    for _ in range(10):
        try:
            result = ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "InvocationDoesNotExist":
                raise
        else:
            state = result["Status"]
            if state == "Success":
                return True
            if state not in ("Pending", "InProgress", "Delayed"):
                logger.warning("Scheduled local health check failed: %s", state)
                return False
        time.sleep(2)
    logger.warning("Scheduled local health check timed out")
    return False


def availability(ec2, ssm, instance_id: str, now: datetime) -> bool:
    expected = expected_state(now)
    if expected == "grace":
        return True
    reservations = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"]
    instances = [instance for reservation in reservations for instance in reservation["Instances"]]
    if len(instances) != 1:
        logger.error("Expected EC2 instance not found")
        return False
    actual = instances[0]["State"]["Name"]
    if actual != expected:
        logger.warning("Expected EC2 state %s, observed %s", expected, actual)
        return False
    return expected != "running" or dashboard_healthy(ssm, instance_id)


def handler(_event, _context):
    instance_id = os.environ["INSTANCE_ID"]
    healthy = availability(
        boto3.client("ec2"),
        boto3.client("ssm"),
        instance_id,
        datetime.now(ZoneInfo("Asia/Kolkata")),
    )
    boto3.client("cloudwatch").put_metric_data(
        Namespace="TradingDashboard",
        MetricData=[
            {
                "MetricName": "ScheduledAvailability",
                "Dimensions": [{"Name": "InstanceId", "Value": instance_id}],
                "Value": 1.0 if healthy else 0.0,
                "Unit": "Count",
            }
        ],
    )
    logger.info("Scheduled availability check: %s", "OK" if healthy else "FAILED")
