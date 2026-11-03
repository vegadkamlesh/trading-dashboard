"""Package and create the private mock deployment using temporary AWS credentials."""

from __future__ import annotations

import argparse
import hashlib
import importlib.resources
import io
import json
import os
import re
import shutil
import subprocess
import uuid
import zipfile
from collections.abc import Callable
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import boto3
from botocore.exceptions import ClientError, WaiterError
from dotenv import dotenv_values

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
REGION = "ap-south-1"
STACK_NAME = re.compile(r"[A-Za-z][A-Za-z0-9-]{0,31}")
UPDATABLE_STACK_STATES = frozenset(("CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE"))


def linux_text(path: Path) -> bytes:
    return path.read_bytes().replace(b"\r\n", b"\n")


def bootstrap_artifact() -> bytes:
    return linux_text(HERE / "bootstrap.sh")


def telegram_credentials(path: Path) -> dict[str, str]:
    values = dotenv_values(path)
    if str(values.get("TELEGRAM_ENABLED", "")).lower() not in ("true", "1", "yes"):
        raise ValueError("Enable Telegram in the local backend configuration before deployment")
    token = values.get("TELEGRAM_BOT_TOKEN") or ""
    chat_id = values.get("TELEGRAM_CHAT_ID") or ""
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
        raise ValueError("Invalid local Telegram bot token format")
    if not re.fullmatch(r"-?[0-9]+|@[A-Za-z][A-Za-z0-9_]{4,}", chat_id):
        raise ValueError("Invalid local Telegram chat ID format")
    return {"token": token, "chat_id": chat_id}


def create_archive(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(files.items()):
            if name.startswith("/") or ".." in Path(name).parts or "\\" in name:
                raise ValueError("Unsafe artifact path")
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content, compress_type=zipfile.ZIP_DEFLATED)
    return buffer.getvalue()


def application_archive() -> bytes:
    files: dict[str, bytes] = {}
    app_dir = ROOT / "backend" / "app"
    for path in app_dir.rglob("*.py"):
        if "__pycache__" not in path.parts:
            files[f"backend/app/{path.relative_to(app_dir).as_posix()}"] = linux_text(path)
    for name in ("requirements.txt", "run.py"):
        files[f"backend/{name}"] = linux_text(ROOT / "backend" / name)
    for name in ("nginx.conf", "trading-dashboard.service", "cloudwatch-agent.json", "write_config.py"):
        files[f"aws/{name}"] = linux_text(HERE / name)
    dist = ROOT / "frontend" / "dist"
    if not (dist / "index.html").is_file():
        raise ValueError("Frontend production build is missing")
    for path in dist.rglob("*"):
        if path.is_file():
            files[f"frontend/dist/{path.relative_to(dist).as_posix()}"] = path.read_bytes()
    return create_archive(files)


def lambda_archive() -> bytes:
    files = {name: linux_text(HERE / name) for name in ("alerts.py", "monitor.py", "cost_report.py")}
    tzdata = importlib.resources.files("tzdata")
    for name in ("__init__.py", "zoneinfo/__init__.py", "zoneinfo/Asia/__init__.py", "zoneinfo/Asia/Kolkata"):
        files[f"tzdata/{name}"] = tzdata.joinpath(*name.split("/")).read_bytes()
    return create_archive(files)


def build_frontend() -> None:
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if not npm:
        raise RuntimeError("npm is required to build the frontend")
    subprocess.run([npm, "run", "build"], cwd=ROOT / "frontend", check=True)


def create_or_verify_bucket(s3, bucket: str, account_id: str, region: str) -> None:
    try:
        s3.head_bucket(Bucket=bucket, ExpectedBucketOwner=account_id)
        exists = True
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in ("404", "NoSuchBucket", "NotFound"):
            raise
        exists = False
    if not exists:
        s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": region})
        s3.put_public_access_block(
            Bucket=bucket,
            PublicAccessBlockConfiguration={
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True,
            },
        )
        s3.put_bucket_encryption(
            Bucket=bucket,
            ServerSideEncryptionConfiguration={
                "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
            },
        )
        arn = f"arn:aws:s3:::{bucket}"
        s3.put_bucket_policy(
            Bucket=bucket,
            Policy=json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Sid": "RequireTLS",
                            "Effect": "Deny",
                            "Principal": "*",
                            "Action": "s3:*",
                            "Resource": [arn, f"{arn}/*"],
                            "Condition": {"Bool": {"aws:SecureTransport": "false"}},
                        }
                    ],
                }
            ),
        )
        print("Created private encrypted artifact bucket")
    else:
        block = s3.get_public_access_block(Bucket=bucket, ExpectedBucketOwner=account_id)
        if not all(block["PublicAccessBlockConfiguration"].values()):
            raise RuntimeError("Existing artifact bucket does not block all public access")
        encryption = s3.get_bucket_encryption(Bucket=bucket, ExpectedBucketOwner=account_id)
        if not encryption["ServerSideEncryptionConfiguration"]["Rules"]:
            raise RuntimeError("Existing artifact bucket does not have default encryption")
        print("Reusing verified private encrypted artifact bucket")


def create_or_verify_secret(ssm, name: str, credentials: dict[str, str]) -> None:
    try:
        parameter = ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ParameterNotFound":
            raise
        ssm.put_parameter(
            Name=name,
            Description="Telegram notification credentials for the private trading dashboard",
            Type="SecureString",
            Value=json.dumps(credentials, separators=(",", ":")),
            Overwrite=False,
        )
        print("Stored Telegram credentials in encrypted Parameter Store")
        return
    if parameter["Type"] != "SecureString" or json.loads(parameter["Value"]) != credentials:
        raise RuntimeError("Existing Telegram parameter differs; refusing to overwrite it")
    print("Reusing matching encrypted Telegram parameter")


def select_network(ec2) -> tuple[str, str, str]:
    vpcs = ec2.describe_vpcs(Filters=[{"Name": "is-default", "Values": ["true"]}])["Vpcs"]
    if len(vpcs) != 1:
        raise RuntimeError("Exactly one default VPC is required")
    vpc_id = vpcs[0]["VpcId"]
    gateways = ec2.describe_internet_gateways(
        Filters=[{"Name": "attachment.vpc-id", "Values": [vpc_id]}]
    )["InternetGateways"]
    gateway_ids = {
        gateway["InternetGatewayId"]
        for gateway in gateways
        if any(attachment.get("State") == "available" for attachment in gateway["Attachments"])
    }
    routes = ec2.describe_route_tables(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["RouteTables"]
    subnets = ec2.describe_subnets(
        Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "default-for-az", "Values": ["true"]},
            {"Name": "state", "Values": ["available"]},
        ]
    )["Subnets"]
    offerings = ec2.describe_instance_type_offerings(
        LocationType="availability-zone",
        Filters=[{"Name": "instance-type", "Values": ["t4g.micro"]}],
    )["InstanceTypeOfferings"]
    offered_zones = {item["Location"] for item in offerings}
    for subnet in sorted(subnets, key=lambda item: item["AvailabilityZone"]):
        if subnet["AvailabilityZone"] not in offered_zones:
            continue
        explicit = [
            table for table in routes
            if any(association.get("SubnetId") == subnet["SubnetId"] for association in table["Associations"])
        ]
        main = [table for table in routes if any(a.get("Main") for a in table["Associations"])]
        selected = explicit or main
        if len(selected) != 1:
            continue
        if any(
            route.get("DestinationCidrBlock") == "0.0.0.0/0"
            and route.get("State") == "active"
            and route.get("GatewayId") in gateway_ids
            for route in selected[0]["Routes"]
        ):
            return vpc_id, subnet["SubnetId"], subnet["AvailabilityZone"]
    raise RuntimeError("No default subnet with an active internet route and t4g.micro offering")


def upload_artifact(s3, bucket: str, prefix: str, name: str, content: bytes) -> str:
    digest = hashlib.sha256(content).hexdigest()
    key = f"{prefix}/{name}-{digest[:20]}"
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=content,
        ServerSideEncryption="AES256",
        Metadata={"sha256": digest},
    )
    return key


def ensure_stack_absent(cloudformation, stack: str) -> None:
    try:
        cloudformation.describe_stacks(StackName=stack)
    except ClientError as exc:
        message = exc.response["Error"].get("Message", "")
        if exc.response["Error"]["Code"] == "ValidationError" and "does not exist" in message:
            return
        raise
    raise RuntimeError(f"CloudFormation stack {stack} already exists; refusing to replace it")


def create_deployment(profile: str, stack: str) -> None:
    if not STACK_NAME.fullmatch(stack):
        raise ValueError("Stack name must be 1-32 letters, digits or hyphens, beginning with a letter")
    credentials = telegram_credentials(ROOT / "backend" / ".env")
    build_frontend()
    app = application_archive()
    functions = lambda_archive()
    bootstrap = bootstrap_artifact()
    template = (HERE / "stack.yaml").read_text(encoding="utf-8")
    session = boto3.Session(profile_name=profile, region_name=REGION)
    account_id = session.client("sts").get_caller_identity()["Account"]
    cloudformation = session.client("cloudformation")
    ensure_stack_absent(cloudformation, stack)
    cloudformation.validate_template(TemplateBody=template)
    vpc, subnet, zone = select_network(session.client("ec2"))

    bucket = f"trading-artifacts-{account_id}-{REGION}"
    parameter = f"/{stack}/telegram"
    s3 = session.client("s3")
    create_or_verify_bucket(s3, bucket, account_id, REGION)
    create_or_verify_secret(session.client("ssm"), parameter, credentials)
    app_key = upload_artifact(s3, bucket, stack, "app", app)
    lambda_key = upload_artifact(s3, bucket, stack, "lambda", functions)
    bootstrap_key = upload_artifact(s3, bucket, stack, "bootstrap", bootstrap)
    parameters = {
        "ArtifactBucket": bucket,
        "AppArtifactKey": app_key,
        "LambdaArtifactKey": lambda_key,
        "BootstrapArtifactKey": bootstrap_key,
        "TelegramParameter": parameter,
        "VpcId": vpc,
        "SubnetId": subnet,
        "AvailabilityZone": zone,
        "AutomationEnabled": "false",
        "CostReportsEnabled": "false",
        "MonthlyBudgetUsd": "20",
    }
    print(f"Creating {stack} in {REGION}; schedules remain disabled and trading uses mock credentials")
    cloudformation.create_stack(
        StackName=stack,
        TemplateBody=template,
        Parameters=[{"ParameterKey": key, "ParameterValue": value} for key, value in parameters.items()],
        Capabilities=["CAPABILITY_NAMED_IAM"],
        Tags=[{"Key": "Application", "Value": "trading-dashboard"}, {"Key": "DeploymentMode", "Value": "mock"}],
    )
    try:
        cloudformation.get_waiter("stack_create_complete").wait(StackName=stack)
    except WaiterError as exc:
        events = cloudformation.describe_stack_events(StackName=stack)["StackEvents"]
        for event in events:
            if event["ResourceStatus"] == "CREATE_FAILED":
                print(f"Creation failed: {event['LogicalResourceId']}: {event.get('ResourceStatusReason', '')}")
        retained = cloudformation.list_stack_resources(StackName=stack)["StackResourceSummaries"]
        for resource in retained:
            if resource["ResourceStatus"] == "DELETE_SKIPPED":
                print(f"Retained resource may still incur charges: {resource['PhysicalResourceId']}")
        raise RuntimeError("Deployment rolled back; inspect the reported AWS errors and retained resources") from exc
    print("CloudFormation created; bootstrap and independent safety checks still need verification")
    for output in cloudformation.describe_stacks(StackName=stack)["Stacks"][0].get("Outputs", []):
        print(f"{output['OutputKey']}: {output['OutputValue']}")


def assert_safe_changes(
    changes: list[dict], *, ebs_backed_instance: bool = False, forbid_instance_change: bool = False
) -> None:
    resources = {change["ResourceChange"]["LogicalResourceId"]: change["ResourceChange"] for change in changes}
    if forbid_instance_change and "DashboardInstance" in resources:
        raise RuntimeError("Refusing to alter the instance while changing automation settings")
    for change in changes:
        resource = change["ResourceChange"]
        action = resource["Action"]
        replacement = resource.get("Replacement")
        print(f"  {action}: {resource['LogicalResourceId']} (replacement={replacement or 'none'})")
        if action not in ("Add", "Modify") or replacement == "True":
            raise RuntimeError(
                f"Refusing a removal or possible replacement of {resource['LogicalResourceId']}"
            )

    stable = {
        name for name, resource in resources.items()
        if resource["Action"] == "Add" or resource.get("Replacement") == "False"
    }
    instance = resources.get("DashboardInstance")
    if instance and instance.get("Replacement") == "Conditional":
        details = instance.get("Details", [])
        if not ebs_backed_instance or not details or any(
            detail.get("Target", {}).get("Name") != "UserData"
            or detail.get("Target", {}).get("RequiresRecreation") != "Conditionally"
            or detail.get("CausingEntity") not in (None, "AppArtifactKey", "BootstrapArtifactKey")
            for detail in details
        ):
            raise RuntimeError("Refusing an instance update other than EBS-backed user data")
        stable.add("DashboardInstance")

    dependents = {
        "AlertSubscription": ("Endpoint", "AlertFunction.Arn", "AlertFunction"),
        "DataAttachment": ("InstanceId", "DashboardInstance", "DashboardInstance"),
        "StaticAddressAssociation": ("InstanceId", "DashboardInstance", "DashboardInstance"),
        "SecondaryNetworkInterfaceAttachment": ("InstanceId", "DashboardInstance", "DashboardInstance"),
        "MonitorInvocationPermission": ("SourceArn", "MonitorSchedule.Arn", "MonitorSchedule"),
    }
    for name, resource in resources.items():
        if resource.get("Replacement") != "Conditional" or name == "DashboardInstance":
            continue
        expected = dependents.get(name)
        if expected is None:
            raise RuntimeError(f"Refusing an unreviewed conditional replacement of {name}")
        target, cause, source = expected
        details = resource.get("Details", [])
        if not details or (source in resources and source not in stable) or any(
            detail.get("Target", {}).get("Name") != target
            or detail.get("CausingEntity") != cause
            or detail.get("ChangeSource") not in ("ResourceReference", "ResourceAttribute")
            for detail in details
        ):
            raise RuntimeError(f"Refusing an unsafe conditional change to {name}")


def assert_safe_detachment(changes: list[dict]) -> None:
    if len(changes) != 1:
        raise RuntimeError("Data-volume detachment must not change any other resource")
    resource = changes[0]["ResourceChange"]
    if (
        resource["Action"] != "Remove"
        or resource["LogicalResourceId"] != "DataAttachment"
        or resource["ResourceType"] != "AWS::EC2::VolumeAttachment"
    ):
        raise RuntimeError("Refusing anything other than the data-volume attachment removal")
    print("  Remove: DataAttachment (the encrypted data volume is retained)")


def assert_safe_mock_replacement(changes: list[dict], *, static_ip: bool = True) -> None:
    resources = {change["ResourceChange"]["LogicalResourceId"]: change["ResourceChange"] for change in changes}
    if "DataVolume" in resources:
        raise RuntimeError("The original data volume must not be modified or replaced")
    instance = resources.get("DashboardInstance")
    if not instance or instance["Action"] != "Modify" or instance.get("Replacement") != "True":
        raise RuntimeError("Expected exactly one replacement of the mock instance")
    if instance.get("ResourceType") != "AWS::EC2::Instance":
        raise RuntimeError("The replacement is not an EC2 instance")
    targets = {detail.get("Target", {}).get("Name") for detail in instance.get("Details", [])}
    if not {"ImageId", "InstanceType"} <= targets or not targets <= {"ImageId", "InstanceType", "UserData"}:
        raise RuntimeError("Refusing unexpected instance replacement properties")
    added = {name for name, resource in resources.items() if resource["Action"] == "Add"}
    if added != {"DataAttachment", "RecoverySafetyStop"}:
        raise RuntimeError("Replacement must add only the data attachment and recovery safety stop")
    if (
        resources["DataAttachment"].get("ResourceType") != "AWS::EC2::VolumeAttachment"
        or resources["RecoverySafetyStop"].get("ResourceType") != "AWS::Scheduler::Schedule"
    ):
        raise RuntimeError("Replacement resources have unexpected types")
    address = resources.get("StaticAddressAssociation")
    if not static_ip:
        if address is not None or "StaticAddress" in resources:
            raise RuntimeError("Refusing a static-address change while the static IP is disabled")
    elif (
        not address or address["Action"] != "Modify"
        or address.get("ResourceType") != "AWS::EC2::EIPAssociation"
    ):
        raise RuntimeError("The existing static address must be reassociated with the replacement")
    if static_ip and (not address.get("Details") or any(
        detail.get("Target", {}).get("Name") != "InstanceId"
        or detail.get("CausingEntity") != "DashboardInstance"
        or detail.get("ChangeSource") != "ResourceReference"
        for detail in address["Details"]
    )):
        raise RuntimeError("Refusing an unexpected static-address association change")

    stable_modifications = {
        "AlertFunction", "MonitorFunction", "CostReportFunction", "InstanceRole", "OperatorRole",
        "MonitorRole", "SchedulerRole", "MonitorSchedule", "CostReportSchedulerRole", "WeekdayCostReport",
        "WeekdayStart", "WeekdayStop", "AvailabilityAlarm", "InstanceStatusAlarm", "MemoryAlarm",
        "RootDiskAlarm", "DataDiskAlarm", "CpuCreditAlarm",
    }
    conditional_dependents = {
        "AlertSubscription": ("Endpoint", "AlertFunction.Arn", "AlertFunction"),
        "MonitorInvocationPermission": ("SourceArn", "MonitorSchedule.Arn", "MonitorSchedule"),
    }
    for name, resource in resources.items():
        action, replacement = resource["Action"], resource.get("Replacement")
        print(f"  {action}: {name} (replacement={replacement or 'none'})")
        if action not in ("Add", "Modify"):
            raise RuntimeError(f"Refusing removal or import of {name}")
        if name in ("DashboardInstance", "DataAttachment", "RecoverySafetyStop", "StaticAddressAssociation"):
            continue
        if action != "Modify":
            raise RuntimeError(f"Refusing an unexpected added resource: {name}")
        if replacement == "False" and name in stable_modifications:
            continue
        expected = conditional_dependents.get(name)
        if replacement != "Conditional" or expected is None:
            raise RuntimeError(f"Refusing an unreviewed replacement or modification of {name}")
        target, cause, source = expected
        source_change = resources.get(source)
        if source_change and source_change.get("Replacement") != "False":
            raise RuntimeError(f"Refusing a dependent change from unstable resource {source}")
        details = resource.get("Details", [])
        if not details or any(
            detail.get("Target", {}).get("Name") != target
            or detail.get("CausingEntity") != cause
            or detail.get("ChangeSource") not in ("ResourceReference", "ResourceAttribute")
            for detail in details
        ):
            raise RuntimeError(f"Refusing an unsafe conditional change to {name}")


def recovery_stop_expression(now: datetime) -> str:
    if now.tzinfo is None:
        raise ValueError("A timezone-aware time is required")
    local = now.astimezone(ZoneInfo("Asia/Kolkata"))
    if local.weekday() >= 5 or local.time() >= time(15, 30):
        raise RuntimeError("Mock replacement must begin on a weekday before 15:30 IST")
    return f"at({local.date().isoformat()}T16:00:00)"


def execute_reviewed_update(
    cloudformation, stack: str, parameters: list[dict], *, template: str | None = None,
    ebs_backed_instance: bool = False, forbid_instance_change: bool = False,
    review: Callable[[list[dict]], None] | None = None, preview_only: bool = False,
) -> None:
    request: dict[str, object] = {
        "StackName": stack,
        "ChangeSetName": f"reviewed-update-{uuid.uuid4().hex[:12]}",
        "ChangeSetType": "UPDATE",
        "Parameters": parameters,
        "Capabilities": ["CAPABILITY_NAMED_IAM"],
    }
    if template is None:
        request["UsePreviousTemplate"] = True
    else:
        request["TemplateBody"] = template
    change_set_id = cloudformation.create_change_set(**request)["Id"]
    try:
        cloudformation.get_waiter("change_set_create_complete").wait(
            StackName=stack, ChangeSetName=change_set_id, WaiterConfig={"Delay": 5, "MaxAttempts": 120}
        )
        change_set = cloudformation.describe_change_set(StackName=stack, ChangeSetName=change_set_id)
        if change_set.get("NextToken"):
            raise RuntimeError("Change set was truncated; refusing an incomplete safety review")
        print("Reviewing AWS change set:")
        if review is None:
            assert_safe_changes(
                change_set["Changes"], ebs_backed_instance=ebs_backed_instance,
                forbid_instance_change=forbid_instance_change,
            )
        else:
            for change in change_set["Changes"]:
                resource = change["ResourceChange"]
                print(
                    f"  {resource['Action']}: {resource['LogicalResourceId']} "
                    f"(replacement={resource.get('Replacement') or 'none'})"
                )
            try:
                review(change_set["Changes"])
            except RuntimeError:
                for change in change_set["Changes"]:
                    resource = change["ResourceChange"]
                    print(
                        f"  {resource['Action']}: {resource['LogicalResourceId']} "
                        f"(replacement={resource.get('Replacement') or 'none'})"
                    )
                    for detail in resource.get("Details", []):
                        target = detail.get("Target", {})
                        print(
                            f"    {target.get('Name')}: recreation={target.get('RequiresRecreation')}, "
                            f"source={detail.get('ChangeSource')}, cause={detail.get('CausingEntity')}"
                        )
                raise
    except (WaiterError, RuntimeError, ClientError):
        cloudformation.delete_change_set(StackName=stack, ChangeSetName=change_set_id)
        raise
    if preview_only:
        cloudformation.delete_change_set(StackName=stack, ChangeSetName=change_set_id)
        print("Preview only: the reviewed change set was deleted without changing AWS")
        return
    cloudformation.execute_change_set(StackName=stack, ChangeSetName=change_set_id)
    cloudformation.get_waiter("stack_update_complete").wait(StackName=stack)


def verified_recovery_point(backup, vault: str, recovery_arn: str, volume_id: str) -> dict:
    point = backup.describe_recovery_point(BackupVaultName=vault, RecoveryPointArn=recovery_arn)
    if (
        point["Status"] != "COMPLETED"
        or not point["IsEncrypted"]
        or point["ResourceArn"].rsplit("/", 1)[-1] != volume_id
    ):
        raise RuntimeError(f"No completed encrypted recovery point for volume {volume_id}")
    return point


def require_empty_mock_snapshot(session, recovery_arn: str) -> None:
    snapshot_id = recovery_arn.rsplit("/", 1)[-1]
    if not re.fullmatch(r"snap-[0-9a-f]+", snapshot_id):
        raise RuntimeError("The data recovery point is not an EBS snapshot")
    blocks = session.client("ebs").list_snapshot_blocks(SnapshotId=snapshot_id, MaxResults=100)
    if blocks["Blocks"] or blocks.get("NextToken"):
        raise RuntimeError("The mock data volume is not empty; refusing automatic replacement")


def detach_mock_data(
    profile: str, stack: str, data_recovery: str, root_recovery: str, confirmed: bool
) -> None:
    if not confirmed:
        raise ValueError("Confirm that replacement of the stopped mock instance was approved")
    if not STACK_NAME.fullmatch(stack):
        raise ValueError("Invalid stack name")
    session = boto3.Session(profile_name=profile, region_name=REGION)
    cloudformation, ec2, backup = session.client("cloudformation"), session.client("ec2"), session.client("backup")
    current = cloudformation.describe_stacks(StackName=stack)["Stacks"][0]
    if current["StackStatus"] not in UPDATABLE_STACK_STATES:
        raise RuntimeError("Stack is not in an updateable state")
    if {tag["Key"]: tag["Value"] for tag in current.get("Tags", [])}.get("DeploymentMode") != "mock":
        raise RuntimeError("Data detachment is restricted to the mock deployment")
    values = {item["ParameterKey"]: item["ParameterValue"] for item in current["Parameters"]}
    if values["AutomationEnabled"] != "false" or values.get("DataAttachmentEnabled", "true") != "true":
        raise RuntimeError("Automation must be disabled and the data attachment enabled")
    outputs = {item["OutputKey"]: item["OutputValue"] for item in current["Outputs"]}
    instance_id, data_volume_id = outputs["InstanceId"], outputs["DataVolumeId"]
    instance = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]
    if instance["State"]["Name"] != "stopped":
        raise RuntimeError("The mock instance must be stopped before detaching its data volume")
    roots = [
        mapping["Ebs"]["VolumeId"] for mapping in instance["BlockDeviceMappings"]
        if mapping["DeviceName"] == instance["RootDeviceName"] and "Ebs" in mapping
    ]
    if len(roots) != 1 or roots[0] == data_volume_id:
        raise RuntimeError("Unable to identify a separate root volume")
    volume = ec2.describe_volumes(VolumeIds=[data_volume_id])["Volumes"][0]
    attachments = volume["Attachments"]
    if (
        not volume["Encrypted"] or len(attachments) != 1
        or attachments[0]["InstanceId"] != instance_id or attachments[0]["State"] != "attached"
    ):
        raise RuntimeError("The encrypted data volume is not exclusively attached to the mock instance")
    verified_recovery_point(backup, outputs["BackupVaultName"], data_recovery, data_volume_id)
    verified_recovery_point(backup, outputs["BackupVaultName"], root_recovery, roots[0])
    require_empty_mock_snapshot(session, data_recovery)

    parameters = [
        {"ParameterKey": "DataAttachmentEnabled", "ParameterValue": "false"}
        if key == "DataAttachmentEnabled" else {"ParameterKey": key, "UsePreviousValue": True}
        for key in values
    ]
    if "DataAttachmentEnabled" not in values:
        parameters.append({"ParameterKey": "DataAttachmentEnabled", "ParameterValue": "false"})
    template = (HERE / "stack.yaml").read_text(encoding="utf-8")
    cloudformation.validate_template(TemplateBody=template)
    execute_reviewed_update(cloudformation, stack, parameters, template=template, review=assert_safe_detachment)
    ec2.get_waiter("volume_available").wait(VolumeIds=[data_volume_id])
    print("Mock data volume safely detached and retained; the original server is still stopped")


def replace_mock_server(
    profile: str, stack: str, data_recovery: str, root_recovery: str, confirmed: bool
) -> None:
    if not confirmed:
        raise ValueError("Confirm that replacement of the stopped mock instance was approved")
    if not STACK_NAME.fullmatch(stack):
        raise ValueError("Invalid stack name")
    stop_at = recovery_stop_expression(datetime.now(ZoneInfo("Asia/Kolkata")))
    session = boto3.Session(profile_name=profile, region_name=REGION)
    cloudformation, ec2, backup = session.client("cloudformation"), session.client("ec2"), session.client("backup")
    current = cloudformation.describe_stacks(StackName=stack)["Stacks"][0]
    if current["StackStatus"] not in UPDATABLE_STACK_STATES:
        raise RuntimeError("Stack is not in an updateable state")
    if {tag["Key"]: tag["Value"] for tag in current.get("Tags", [])}.get("DeploymentMode") != "mock":
        raise RuntimeError("Instance replacement is restricted to the mock deployment")
    values = {item["ParameterKey"]: item["ParameterValue"] for item in current["Parameters"]}
    if values["AutomationEnabled"] != "false" or values.get("DataAttachmentEnabled") != "false":
        raise RuntimeError("Automation must be disabled and the mock data volume detached")
    outputs = {item["OutputKey"]: item["OutputValue"] for item in current["Outputs"]}
    old_instance_id, data_volume_id = outputs["InstanceId"], outputs["DataVolumeId"]
    instance = ec2.describe_instances(InstanceIds=[old_instance_id])["Reservations"][0]["Instances"][0]
    if instance["State"]["Name"] != "stopped" or instance["RootDeviceType"] != "ebs":
        raise RuntimeError("The original mock instance must be stopped and EBS-backed")
    group_ids = [group["GroupId"] for group in instance["SecurityGroups"]]
    groups = ec2.describe_security_groups(GroupIds=group_ids)["SecurityGroups"]
    if not groups or any(group["IpPermissions"] for group in groups):
        raise RuntimeError("The mock security groups must not allow inbound access")
    roots = [
        mapping["Ebs"]["VolumeId"] for mapping in instance["BlockDeviceMappings"]
        if mapping["DeviceName"] == instance["RootDeviceName"] and "Ebs" in mapping
    ]
    if len(roots) != 1 or roots[0] == data_volume_id:
        raise RuntimeError("Unable to identify a separate root volume")
    volume = ec2.describe_volumes(VolumeIds=[data_volume_id])["Volumes"][0]
    if (
        not volume["Encrypted"] or volume["State"] != "available" or volume["Attachments"]
        or volume["AvailabilityZone"] != instance["Placement"]["AvailabilityZone"]
    ):
        raise RuntimeError("The original encrypted data volume is not safely detached in the same zone")
    verified_recovery_point(backup, outputs["BackupVaultName"], data_recovery, data_volume_id)
    verified_recovery_point(backup, outputs["BackupVaultName"], root_recovery, roots[0])
    require_empty_mock_snapshot(session, data_recovery)

    instance_type = "t3.micro"
    instance_info = ec2.describe_instance_types(InstanceTypes=[instance_type])["InstanceTypes"][0]
    if not instance_info.get("FreeTierEligible") or instance_info["MemoryInfo"]["SizeInMiB"] != 1024:
        raise RuntimeError("The approved replacement is not a Free-plan-compatible 1 GiB instance")
    offerings = ec2.describe_instance_type_offerings(
        LocationType="availability-zone",
        Filters=[
            {"Name": "instance-type", "Values": [instance_type]},
            {"Name": "location", "Values": [volume["AvailabilityZone"]]},
        ],
    )["InstanceTypeOfferings"]
    if not offerings:
        raise RuntimeError("The approved instance type is not offered in the data-volume zone")
    image_parameter = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
    image_id = session.client("ssm").get_parameter(Name=image_parameter)["Parameter"]["Value"]
    images = ec2.describe_images(ImageIds=[image_id], Owners=["amazon"])["Images"]
    if len(images) != 1 or images[0]["Architecture"] != "x86_64" or images[0]["State"] != "available":
        raise RuntimeError("The official x86-64 Amazon Linux image is not available")

    account_id = session.client("sts").get_caller_identity()["Account"]
    bucket = values["ArtifactBucket"]
    s3 = session.client("s3")
    create_or_verify_bucket(s3, bucket, account_id, REGION)
    create_or_verify_secret(
        session.client("ssm"), values["TelegramParameter"], telegram_credentials(ROOT / "backend" / ".env")
    )
    build_frontend()
    updates = {
        "AppArtifactKey": upload_artifact(s3, bucket, stack, "app", application_archive()),
        "BootstrapArtifactKey": upload_artifact(s3, bucket, stack, "bootstrap", bootstrap_artifact()),
        "DataAttachmentEnabled": "true",
        "ImageId": image_parameter,
        "InstanceType": instance_type,
        "RecoveryStopAt": stop_at,
    }
    parameters = [
        {"ParameterKey": key, "ParameterValue": updates[key]}
        if key in updates else {"ParameterKey": key, "UsePreviousValue": True}
        for key in values
    ]
    parameters.extend(
        {"ParameterKey": key, "ParameterValue": updates[key]}
        for key in ("InstanceType", "RecoveryStopAt") if key not in values
    )
    template = (HERE / "stack.yaml").read_text(encoding="utf-8")
    cloudformation.validate_template(TemplateBody=template)

    def review(changes: list[dict]) -> None:
        assert_safe_mock_replacement(changes, static_ip=values.get("StaticIpEnabled", "true") == "true")
        if recovery_stop_expression(datetime.now(ZoneInfo("Asia/Kolkata"))) != stop_at:
            raise RuntimeError("The recovery safety-stop window changed during review")

    execute_reviewed_update(cloudformation, stack, parameters, template=template, review=review)
    updated = cloudformation.describe_stacks(StackName=stack)["Stacks"][0]
    new_outputs = {item["OutputKey"]: item["OutputValue"] for item in updated["Outputs"]}
    new_id = new_outputs["InstanceId"]
    if new_id == old_instance_id or new_outputs["DataVolumeId"] != data_volume_id:
        raise RuntimeError("The replacement did not preserve the expected data volume")
    if new_outputs.get("StaticOutboundIp") != outputs.get("StaticOutboundIp"):
        raise RuntimeError("The replacement changed the configured static outbound IP")
    attached = ec2.describe_volumes(VolumeIds=[data_volume_id])["Volumes"][0]
    if len(attached["Attachments"]) != 1 or attached["Attachments"][0]["InstanceId"] != new_id:
        raise RuntimeError("The original data volume is not attached to the replacement")
    schedule = session.client("scheduler").get_schedule(Name=f"{stack}-recovery-safety-stop")
    target = json.loads(schedule["Target"]["Input"])
    if (
        schedule["State"] != "ENABLED" or schedule["ScheduleExpression"] != stop_at
        or schedule["ScheduleExpressionTimezone"] != "Asia/Kolkata" or target["InstanceIds"] != [new_id]
    ):
        raise RuntimeError("The replacement's independent 16:00 safety stop is not configured correctly")
    print(f"Mock instance replaced: {new_id}; original encrypted data and static IP preserved")
    print("The 16:00 safety stop is enabled; bootstrap and production safety checks remain required")


def mock_release_command(bucket: str, key: str, digest: str, volume_id: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
        raise ValueError("Invalid artifact bucket")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,31}/app-[a-f0-9]{20}", key):
        raise ValueError("Invalid mock application artifact key")
    if not re.fullmatch(r"[a-f0-9]{64}", digest) or not key.endswith(digest[:20]):
        raise ValueError("Mock artifact digest does not match its key")
    if not re.fullmatch(r"vol-[a-f0-9]+", volume_id):
        raise ValueError("Invalid data volume ID")
    script = linux_text(HERE / "apply_mock_release.sh").decode("utf-8")
    delimiter = "TRADING_MOCK_RELEASE_SCRIPT"
    if delimiter in script:
        raise RuntimeError("Release script contains the command delimiter")
    return (
        f"bash -s -- '{bucket}' '{key}' '{digest}' '{volume_id}' <<'{delimiter}'\n"
        f"{script}\n{delimiter}"
    )


def verified_mock_release_context(session, stack: str, data_recovery: str, instance_state: str) -> tuple[dict, dict]:
    expected_stop = recovery_stop_expression(datetime.now(ZoneInfo("Asia/Kolkata")))
    cloudformation, ec2 = session.client("cloudformation"), session.client("ec2")
    current = cloudformation.describe_stacks(StackName=stack)["Stacks"][0]
    if current["StackStatus"] not in UPDATABLE_STACK_STATES:
        raise RuntimeError("Stack is not in an updateable state")
    if {tag["Key"]: tag["Value"] for tag in current.get("Tags", [])}.get("DeploymentMode") != "mock":
        raise RuntimeError("Application releases are restricted to the mock deployment")
    values = {item["ParameterKey"]: item["ParameterValue"] for item in current["Parameters"]}
    if (
        values["AutomationEnabled"] != "false"
        or values.get("DataAttachmentEnabled") != "true"
        or values.get("RecoveryStopAt") != expected_stop
    ):
        raise RuntimeError("Disable normal automation, attach the data volume and configure today's safety stop")
    outputs = {item["OutputKey"]: item["OutputValue"] for item in current["Outputs"]}
    instance_id, volume_id = outputs["InstanceId"], outputs["DataVolumeId"]
    instance = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]
    if instance["State"]["Name"] != instance_state or instance["RootDeviceType"] != "ebs":
        raise RuntimeError(f"The mock instance must be {instance_state} and EBS-backed")
    groups = ec2.describe_security_groups(
        GroupIds=[group["GroupId"] for group in instance["SecurityGroups"]]
    )["SecurityGroups"]
    if not groups or any(group["IpPermissions"] for group in groups):
        raise RuntimeError("The mock security groups must not allow inbound access")
    volume = ec2.describe_volumes(VolumeIds=[volume_id])["Volumes"][0]
    if (
        not volume["Encrypted"] or len(volume["Attachments"]) != 1
        or volume["Attachments"][0]["InstanceId"] != instance_id
        or volume["Attachments"][0]["State"] != "attached"
    ):
        raise RuntimeError("The encrypted mock data volume must be exclusively attached")
    verified_recovery_point(session.client("backup"), outputs["BackupVaultName"], data_recovery, volume_id)
    schedule = session.client("scheduler").get_schedule(Name=f"{stack}-recovery-safety-stop")
    target = schedule["Target"]
    try:
        target_input = json.loads(target["Input"])
    except (KeyError, json.JSONDecodeError) as exc:
        raise RuntimeError("The recovery safety-stop input is invalid") from exc
    if (
        schedule["State"] != "ENABLED"
        or schedule["ScheduleExpression"] != expected_stop
        or schedule["ScheduleExpressionTimezone"] != "Asia/Kolkata"
        or schedule["FlexibleTimeWindow"]["Mode"] != "OFF"
        or not target["Arn"].endswith(":scheduler:::aws-sdk:ec2:stopInstances")
        or target_input != {"InstanceIds": [instance_id]}
    ):
        raise RuntimeError("The current mock instance does not have the expected 16:00 safety stop")
    return values, outputs


def assert_safe_mock_app_pointer_update(changes: list[dict]) -> None:
    allowed = {
        "AvailabilityAlarm": {("Dimensions", "ResourceReference", "DashboardInstance")},
        "CostReportFunction": {("Environment", "ResourceReference", "DashboardInstance")},
        "CostReportSchedulerRole": {("Policies", "ResourceAttribute", "CostReportFunction.Arn")},
        "CpuCreditAlarm": {("Dimensions", "ResourceReference", "DashboardInstance")},
        "DashboardInstance": {
            ("UserData", "DirectModification", None), ("UserData", "ParameterReference", "AppArtifactKey"),
        },
        "DataAttachment": {("InstanceId", "ResourceReference", "DashboardInstance")},
        "DataDiskAlarm": {("Dimensions", "ResourceReference", "DashboardInstance")},
        "InstanceRole": {
            ("Policies", "DirectModification", None), ("Policies", "ParameterReference", "AppArtifactKey"),
        },
        "InstanceStatusAlarm": {("Dimensions", "ResourceReference", "DashboardInstance")},
        "MemoryAlarm": {("Dimensions", "ResourceReference", "DashboardInstance")},
        "MonitorFunction": {
            ("Environment", "ResourceReference", "DashboardInstance"),
            ("Role", "ResourceAttribute", "MonitorRole.Arn"),
        },
        "MonitorInvocationPermission": {("SourceArn", "ResourceAttribute", "MonitorSchedule.Arn")},
        "MonitorRole": {("Policies", "ResourceReference", "DashboardInstance")},
        "MonitorSchedule": {("Targets", "ResourceAttribute", "MonitorFunction.Arn")},
        "OperatorRole": {("Policies", "ResourceReference", "DashboardInstance")},
        "OperatorUserPolicy": {("PolicyDocument", "ResourceAttribute", "OperatorRole.Arn")},
        "RecoverySafetyStop": {
            ("Target", "ResourceReference", "DashboardInstance"),
            ("Target", "ResourceAttribute", "SchedulerRole.Arn"),
        },
        "RootDiskAlarm": {("Dimensions", "ResourceReference", "DashboardInstance")},
        "SchedulerRole": {("Policies", "ResourceReference", "DashboardInstance")},
        "StaticAddressAssociation": {("InstanceId", "ResourceReference", "DashboardInstance")},
        "SecondaryNetworkInterfaceAttachment": {("InstanceId", "ResourceReference", "DashboardInstance")},
        "WeekdayCostReport": {
            ("Target", "ResourceAttribute", "CostReportFunction.Arn"),
            ("Target", "ResourceAttribute", "CostReportSchedulerRole.Arn"),
        },
        "WeekdayStart": {
            ("Target", "ResourceReference", "DashboardInstance"),
            ("Target", "ResourceAttribute", "SchedulerRole.Arn"),
        },
        "WeekdayStop": {
            ("Target", "ResourceReference", "DashboardInstance"),
            ("Target", "ResourceAttribute", "SchedulerRole.Arn"),
        },
    }
    assert_safe_changes(changes, ebs_backed_instance=True)
    instance_seen = False
    for change in changes:
        resource = change["ResourceChange"]
        name = resource["LogicalResourceId"]
        if resource["Action"] != "Modify" or name not in allowed:
            raise RuntimeError(f"Refusing an unexpected app-pointer change to {name}")
        details = resource.get("Details", [])
        if not details or any(
            (detail.get("Target", {}).get("Name"), detail.get("ChangeSource"), detail.get("CausingEntity"))
            not in allowed[name] for detail in details
        ):
            raise RuntimeError(f"Refusing an unreviewed app-pointer change to {name}")
        if name == "DashboardInstance":
            instance_seen = any(
                detail.get("ChangeSource") == "ParameterReference"
                and detail.get("CausingEntity") == "AppArtifactKey" for detail in details
            )
    if not instance_seen:
        raise RuntimeError("Expected the app artifact key to update the instance user data")


def assert_safe_mock_lambda_pointer_update(changes: list[dict]) -> None:
    functions = {"AlertFunction", "MonitorFunction", "CostReportFunction"}
    code = {
        ("Code", "ParameterReference", "LambdaArtifactKey"),
        ("Code", "DirectModification", None),
    }
    allowed = {
        "AlertFunction": ("AWS::Lambda::Function", "False", code),
        "AlertSubscription": (
            "AWS::SNS::Subscription", "Conditional",
            {("Endpoint", "ResourceAttribute", "AlertFunction.Arn")},
        ),
        "CostReportFunction": ("AWS::Lambda::Function", "False", code),
        "CostReportSchedulerRole": (
            "AWS::IAM::Role", "False",
            {("Policies", "ResourceAttribute", "CostReportFunction.Arn")},
        ),
        "MonitorFunction": ("AWS::Lambda::Function", "False", code),
        "MonitorInvocationPermission": (
            "AWS::Lambda::Permission", "Conditional",
            {("SourceArn", "ResourceAttribute", "MonitorSchedule.Arn")},
        ),
        "MonitorSchedule": (
            "AWS::Events::Rule", "False",
            {("Targets", "ResourceAttribute", "MonitorFunction.Arn")},
        ),
        "WeekdayCostReport": (
            "AWS::Scheduler::Schedule", "False",
            {
                ("Target", "ResourceAttribute", "CostReportFunction.Arn"),
                ("Target", "ResourceAttribute", "CostReportSchedulerRole.Arn"),
            },
        ),
    }
    seen = set()
    for change in changes:
        resource = change["ResourceChange"]
        name = resource["LogicalResourceId"]
        rule = allowed.get(name)
        if (
            rule is None
            or name in seen
            or resource["Action"] != "Modify"
            or resource.get("ResourceType") != rule[0]
            or resource.get("Replacement") != rule[1]
        ):
            raise RuntimeError(f"Refusing an unexpected Lambda artifact change to {name}")
        details = resource.get("Details", [])
        if not details or any(
            (detail.get("Target", {}).get("Name"), detail.get("ChangeSource"), detail.get("CausingEntity"))
            not in rule[2] for detail in details
        ):
            raise RuntimeError(f"Refusing an unreviewed Lambda artifact change to {name}")
        if name in functions and not any(
            detail.get("ChangeSource") == "ParameterReference"
            and detail.get("CausingEntity") == "LambdaArtifactKey" for detail in details
        ):
            raise RuntimeError(f"Expected the Lambda artifact parameter to change {name}")
        seen.add(name)
    if not functions.issubset(seen):
        raise RuntimeError("Expected all three Lambda functions to use the updated artifact")


def update_mock_lambda_artifact(profile: str, stack: str, confirmed: bool) -> None:
    if not confirmed:
        raise ValueError("Confirm that the isolated mock Lambda update was approved")
    if not STACK_NAME.fullmatch(stack):
        raise ValueError("Invalid stack name")
    session = boto3.Session(profile_name=profile, region_name=REGION)
    cloudformation = session.client("cloudformation")
    current = cloudformation.describe_stacks(StackName=stack)["Stacks"][0]
    if current["StackStatus"] not in UPDATABLE_STACK_STATES:
        raise RuntimeError("Stack is not in an updateable state")
    if {tag["Key"]: tag["Value"] for tag in current.get("Tags", [])}.get("DeploymentMode") != "mock":
        raise RuntimeError("Lambda artifact updates are restricted to the mock deployment")
    values = {item["ParameterKey"]: item["ParameterValue"] for item in current["Parameters"]}
    if "LambdaArtifactKey" not in values or "ArtifactBucket" not in values:
        raise RuntimeError("The stack does not expose the expected Lambda artifact parameters")
    account_id = session.client("sts").get_caller_identity()["Account"]
    s3 = session.client("s3")
    create_or_verify_bucket(s3, values["ArtifactBucket"], account_id, REGION)
    key = upload_artifact(s3, values["ArtifactBucket"], stack, "lambda", lambda_archive())
    if key == values["LambdaArtifactKey"]:
        print("The stack already points to this immutable Lambda artifact")
        return
    params = [
        {"ParameterKey": name, "ParameterValue": key}
        if name == "LambdaArtifactKey" else {"ParameterKey": name, "UsePreviousValue": True}
        for name in values
    ]
    execute_reviewed_update(cloudformation, stack, params, review=assert_safe_mock_lambda_pointer_update)
    print("Mock Lambda artifact updated without changing instance or schedules")


def assert_safe_recovery_stop_removal(changes: list[dict]) -> None:
    if len(changes) != 1:
        raise RuntimeError("Refusing changes other than removal of the temporary recovery stop")
    resource = changes[0]["ResourceChange"]
    if (
        resource["Action"] != "Remove"
        or resource["LogicalResourceId"] != "RecoverySafetyStop"
        or resource.get("ResourceType") != "AWS::Scheduler::Schedule"
    ):
        raise RuntimeError("Refusing changes other than removal of the temporary recovery stop")


def remove_recovery_stop(profile: str, stack: str) -> None:
    if not STACK_NAME.fullmatch(stack):
        raise ValueError("Invalid stack name")
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    if time(15, 30) <= now.time() < time(16, 15):
        raise RuntimeError("Do not remove the recovery stop near the 16:00 shutdown")
    session = boto3.Session(profile_name=profile, region_name=REGION)
    cloudformation = session.client("cloudformation")
    current = cloudformation.describe_stacks(StackName=stack)["Stacks"][0]
    if current["StackStatus"] not in UPDATABLE_STACK_STATES:
        raise RuntimeError("Stack is not in an updateable state")
    if {tag["Key"]: tag["Value"] for tag in current.get("Tags", [])}.get("DeploymentMode") != "mock":
        raise RuntimeError("Recovery-stop removal is restricted to the mock deployment")
    values = {item["ParameterKey"]: item["ParameterValue"] for item in current["Parameters"]}
    if values.get("AutomationEnabled") != "true" or not values.get("RecoveryStopAt"):
        raise RuntimeError("Recurring automation must be enabled and a recovery stop must exist")
    outputs = {item["OutputKey"]: item["OutputValue"] for item in current["Outputs"]}
    instance_id = outputs["InstanceId"]
    schedule = session.client("scheduler").get_schedule(Name=f"{stack}-weekday-stop")
    target = schedule["Target"]
    try:
        target_input = json.loads(target["Input"])
    except (KeyError, json.JSONDecodeError) as exc:
        raise RuntimeError("The recurring stop input is invalid") from exc
    if (
        schedule["State"] != "ENABLED"
        or schedule["ScheduleExpression"] != "cron(0 16 ? * MON-FRI *)"
        or schedule["ScheduleExpressionTimezone"] != "Asia/Kolkata"
        or schedule["FlexibleTimeWindow"]["Mode"] != "OFF"
        or not target["Arn"].endswith(":scheduler:::aws-sdk:ec2:stopInstances")
        or target_input != {"InstanceIds": [instance_id]}
    ):
        raise RuntimeError("The recurring 16:00 stop is not enabled for the current instance")
    params = [
        {"ParameterKey": name, "ParameterValue": ""}
        if name == "RecoveryStopAt" else {"ParameterKey": name, "UsePreviousValue": True}
        for name in values
    ]
    execute_reviewed_update(cloudformation, stack, params, review=assert_safe_recovery_stop_removal)
    print("Removed the one-time recovery stop; the recurring 16:00 stop remains enabled")


def assert_safe_mock_operator_update(changes: list[dict]) -> None:
    allowed = {
        "OperatorUser": ("Add", "AWS::IAM::User"),
        "OperatorUserPolicy": ("Add", "AWS::IAM::Policy"),
        "OperatorRole": ("Modify", "AWS::IAM::Role"),
    }
    role_details = {
        ("AssumeRolePolicyDocument", "DirectModification", None),
        ("AssumeRolePolicyDocument", "ResourceAttribute", "OperatorUser.Arn"),
        ("Policies", "DirectModification", None),
    }
    seen = set()
    for change in changes:
        resource = change["ResourceChange"]
        name = resource["LogicalResourceId"]
        expected = allowed.get(name)
        if (
            expected is None
            or name in seen
            or resource["Action"] != expected[0]
            or resource.get("ResourceType") != expected[1]
        ):
            raise RuntimeError(f"Refusing an unexpected operator setup change to {name}")
        if name == "OperatorRole":
            details = resource.get("Details", [])
            if resource.get("Replacement") != "False" or not details or any(
                (detail.get("Target", {}).get("Name"), detail.get("ChangeSource"), detail.get("CausingEntity"))
                not in role_details for detail in details
            ):
                raise RuntimeError("Refusing an unreviewed operator role change")
        seen.add(name)
    if seen != set(allowed):
        raise RuntimeError("Expected the operator user, user policy and restricted role")


def setup_mock_operator(profile: str, stack: str, confirmed: bool) -> None:
    if not confirmed:
        raise ValueError("Confirm that the isolated mock operator setup was approved")
    if not STACK_NAME.fullmatch(stack):
        raise ValueError("Invalid stack name")
    session = boto3.Session(profile_name=profile, region_name=REGION)
    cloudformation = session.client("cloudformation")
    current = cloudformation.describe_stacks(StackName=stack)["Stacks"][0]
    if current["StackStatus"] not in UPDATABLE_STACK_STATES:
        raise RuntimeError("Stack is not in an updateable state")
    if {tag["Key"]: tag["Value"] for tag in current.get("Tags", [])}.get("DeploymentMode") != "mock":
        raise RuntimeError("Automatic operator setup is restricted to the mock deployment")
    template = (HERE / "stack.yaml").read_text(encoding="utf-8")
    if cloudformation.get_template(StackName=stack, TemplateStage="Original")["TemplateBody"] == template:
        print("The stack already uses this operator template")
        return
    try:
        session.client("iam").get_user(UserName=f"{stack}-operator")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "NoSuchEntity":
            raise
    else:
        raise RuntimeError("The intended operator user already exists outside this reviewed update")
    cloudformation.validate_template(TemplateBody=template)
    params = [{"ParameterKey": item["ParameterKey"], "UsePreviousValue": True} for item in current["Parameters"]]
    execute_reviewed_update(
        cloudformation, stack, params, template=template, review=assert_safe_mock_operator_update
    )
    print("Operator identity created without a password or MFA; owner enrollment is required")


def assert_safe_pause_automation(changes: list[dict]) -> None:
    allowed = {
        "AvailabilityAlarm": (
            "AWS::CloudWatch::Alarm", "False",
            {("ActionsEnabled", "DirectModification", None), ("ActionsEnabled", "ParameterReference", "AutomationEnabled")},
        ),
        "MonitorSchedule": (
            "AWS::Events::Rule", "False",
            {("State", "DirectModification", None), ("State", "ParameterReference", "AutomationEnabled")},
        ),
        "MonitorInvocationPermission": (
            "AWS::Lambda::Permission", "Conditional",
            {("SourceArn", "ResourceAttribute", "MonitorSchedule.Arn")},
        ),
        "WeekdayStart": (
            "AWS::Scheduler::Schedule", "False",
            {("State", "DirectModification", None), ("State", "ParameterReference", "AutomationEnabled")},
        ),
        "WeekdayStop": (
            "AWS::Scheduler::Schedule", "False",
            {("State", "DirectModification", None), ("State", "ParameterReference", "AutomationEnabled")},
        ),
    }
    seen = set()
    for change in changes:
        resource = change["ResourceChange"]
        name = resource["LogicalResourceId"]
        rule = allowed.get(name)
        if (
            rule is None
            or name in seen
            or resource["Action"] != "Modify"
            or resource.get("ResourceType") != rule[0]
            or resource.get("Replacement") != rule[1]
        ):
            raise RuntimeError(f"Refusing an unexpected automation pause change to {name}")
        details = resource.get("Details", [])
        if not details or any(
            (detail.get("Target", {}).get("Name"), detail.get("ChangeSource"), detail.get("CausingEntity"))
            not in rule[2] for detail in details
        ):
            raise RuntimeError(f"Refusing an unreviewed automation pause change to {name}")
        seen.add(name)
    if not {"WeekdayStart", "WeekdayStop", "MonitorSchedule", "AvailabilityAlarm"}.issubset(seen):
        raise RuntimeError("Expected the recurring schedules and health monitor to be paused")


def verify_paused_automation(session, stack: str) -> None:
    scheduler = session.client("scheduler")
    for suffix in ("weekday-start", "weekday-stop"):
        if scheduler.get_schedule(Name=f"{stack}-{suffix}")["State"] != "DISABLED":
            raise RuntimeError(f"The {suffix} schedule is not disabled")
    if session.client("events").describe_rule(Name=f"{stack}-monitor")["State"] != "DISABLED":
        raise RuntimeError("The scheduled health monitor is not disabled")
    alarms = session.client("cloudwatch").describe_alarms(AlarmNames=[f"{stack}-availability"])["MetricAlarms"]
    if len(alarms) != 1 or alarms[0]["ActionsEnabled"]:
        raise RuntimeError("The scheduled availability alarm actions are not disabled")


def pause_automation(profile: str, stack: str) -> None:
    if not STACK_NAME.fullmatch(stack):
        raise ValueError("Invalid stack name")
    session = boto3.Session(profile_name=profile, region_name=REGION)
    cloudformation = session.client("cloudformation")
    current = cloudformation.describe_stacks(StackName=stack)["Stacks"][0]
    if current["StackStatus"] not in UPDATABLE_STACK_STATES:
        raise RuntimeError("Stack is not in an updateable state")
    if {tag["Key"]: tag["Value"] for tag in current.get("Tags", [])}.get("DeploymentMode") != "mock":
        raise RuntimeError("Automatic pause is restricted to the mock deployment")
    outputs = {item["OutputKey"]: item["OutputValue"] for item in current["Outputs"]}
    instance = session.client("ec2").describe_instances(InstanceIds=[outputs["InstanceId"]])
    state = instance["Reservations"][0]["Instances"][0]["State"]["Name"]
    if state != "stopped":
        raise RuntimeError("Stop the mock instance before disabling its recurring 16:00 stop")
    values = {item["ParameterKey"]: item["ParameterValue"] for item in current["Parameters"]}
    if values.get("AutomationEnabled") == "false":
        verify_paused_automation(session, stack)
        print("Weekday automation is already paused")
        return
    if values.get("AutomationEnabled") != "true":
        raise RuntimeError("Unexpected automation setting")
    params = [
        {"ParameterKey": name, "ParameterValue": "false"}
        if name == "AutomationEnabled" else {"ParameterKey": name, "UsePreviousValue": True}
        for name in values
    ]
    execute_reviewed_update(cloudformation, stack, params, review=assert_safe_pause_automation)
    verify_paused_automation(session, stack)
    print("Weekday start, stop and scheduled health monitoring paused; cost reports are unchanged")


def assert_safe_static_ip_release(changes: list[dict]) -> None:
    expected = {"StaticAddress": "AWS::EC2::EIP", "StaticAddressAssociation": "AWS::EC2::EIPAssociation"}
    seen = {}
    for change in changes:
        resource = change["ResourceChange"]
        name = resource["LogicalResourceId"]
        if (
            expected.get(name) != resource.get("ResourceType")
            or resource["Action"] != "Remove"
            or name in seen
        ):
            raise RuntimeError(f"Refusing an unexpected static-IP release change to {name}")
        seen[name] = resource
    if set(seen) != set(expected):
        raise RuntimeError("Expected only the Elastic IP and its association to be removed")


def release_static_ip(profile: str, stack: str, confirmed: bool) -> None:
    if not confirmed:
        raise RuntimeError("Releasing the static IP is irreversible; confirm the owner approved it")
    if not STACK_NAME.fullmatch(stack):
        raise ValueError("Invalid stack name")
    session = boto3.Session(profile_name=profile, region_name=REGION)
    cloudformation = session.client("cloudformation")
    current = cloudformation.describe_stacks(StackName=stack)["Stacks"][0]
    if current["StackStatus"] not in UPDATABLE_STACK_STATES:
        raise RuntimeError("Stack is not in an updateable state")
    if {tag["Key"]: tag["Value"] for tag in current.get("Tags", [])}.get("DeploymentMode") != "mock":
        raise RuntimeError("The static IP may be released only for the mock deployment")
    outputs = {item["OutputKey"]: item["OutputValue"] for item in current["Outputs"]}
    values = {item["ParameterKey"]: item["ParameterValue"] for item in current["Parameters"]}
    if values.get("StaticIpEnabled") == "false" and "StaticOutboundIp" not in outputs:
        print("The static IP is already released")
        return
    ec2 = session.client("ec2")
    instance = ec2.describe_instances(InstanceIds=[outputs["InstanceId"]])["Reservations"][0]["Instances"][0]
    if instance["State"]["Name"] != "stopped":
        raise RuntimeError("Stop the mock instance before releasing its static IP")
    template = (HERE / "stack.yaml").read_text(encoding="utf-8")
    cloudformation.validate_template(TemplateBody=template)
    params = [
        {"ParameterKey": name, "UsePreviousValue": True} for name in values if name != "StaticIpEnabled"
    ]
    params.append({"ParameterKey": "StaticIpEnabled", "ParameterValue": "false"})
    execute_reviewed_update(cloudformation, stack, params, template=template, review=assert_safe_static_ip_release)
    updated = cloudformation.describe_stacks(StackName=stack)["Stacks"][0]
    if any(item["OutputKey"] == "StaticOutboundIp" for item in updated["Outputs"]):
        raise RuntimeError("The static IP output still exists after the release")
    remaining = ec2.describe_addresses(
        Filters=[{"Name": "tag:aws:cloudformation:stack-name", "Values": [stack]}]
    )["Addresses"]
    if remaining:
        raise RuntimeError("A stack-owned Elastic IP is still allocated")
    print(f"Static IP {outputs.get('StaticOutboundIp')} released; a new IP must be registered before live trading")


PRIMARY_STATIC_IP_RESOURCES = {
    "StaticAddress": "AWS::EC2::EIP",
    "StaticAddressAssociation": "AWS::EC2::EIPAssociation",
}
SECONDARY_STATIC_IP_RESOURCES = {
    "SecondaryNetworkInterface": "AWS::EC2::NetworkInterface",
    "SecondaryNetworkInterfaceAttachment": "AWS::EC2::NetworkInterfaceAttachment",
    "SecondaryStaticAddress": "AWS::EC2::EIP",
    "SecondaryStaticAddressAssociation": "AWS::EC2::EIPAssociation",
}


def static_ip_resources(count: int) -> dict[str, str]:
    if count not in (0, 1, 2):
        raise ValueError("Static IP count must be 0, 1 or 2")
    resources: dict[str, str] = {}
    if count >= 1:
        resources.update(PRIMARY_STATIC_IP_RESOURCES)
    if count == 2:
        resources.update(SECONDARY_STATIC_IP_RESOURCES)
    return resources


def static_ip_parameters(count: int) -> dict[str, str]:
    static_ip_resources(count)
    return {
        "StaticIpEnabled": "true" if count >= 1 else "false",
        "SecondaryStaticIpEnabled": "true" if count == 2 else "false",
    }


def current_static_ip_count(values: dict[str, str]) -> int:
    primary = values.get("StaticIpEnabled", "true") == "true"
    secondary = values.get("SecondaryStaticIpEnabled", "false") == "true"
    return 2 if primary and secondary else int(primary)


def assert_safe_static_ip_change(changes: list[dict], current: int, target: int) -> None:
    before, after = static_ip_resources(current), static_ip_resources(target)
    expected = {name: ("Add", kind) for name, kind in after.items() if name not in before}
    expected.update({name: ("Remove", kind) for name, kind in before.items() if name not in after})
    seen = set()
    for change in changes:
        resource = change["ResourceChange"]
        name = resource["LogicalResourceId"]
        if expected.get(name) != (resource["Action"], resource.get("ResourceType")) or name in seen:
            raise RuntimeError(f"Refusing an unexpected static-IP change to {name}")
        seen.add(name)
    if seen != set(expected):
        raise RuntimeError(f"Expected exactly these static-IP changes: {sorted(expected)}")


def set_static_ips(profile: str, stack: str, count: int, approved: bool, preview: bool) -> None:
    if not STACK_NAME.fullmatch(stack):
        raise ValueError("Invalid stack name")
    target = static_ip_parameters(count)
    if not preview and not approved:
        raise RuntimeError("Elastic IPs are billed every hour until released; confirm the owner approved it")
    session = boto3.Session(profile_name=profile, region_name=REGION)
    cloudformation = session.client("cloudformation")
    current_stack = cloudformation.describe_stacks(StackName=stack)["Stacks"][0]
    if current_stack["StackStatus"] not in UPDATABLE_STACK_STATES:
        raise RuntimeError("Stack is not in an updateable state")
    values = {item["ParameterKey"]: item["ParameterValue"] for item in current_stack["Parameters"]}
    outputs = {item["OutputKey"]: item["OutputValue"] for item in current_stack["Outputs"]}
    current = current_static_ip_count(values)
    if current == count:
        print(f"The stack already has {count} static IP(s)")
        return
    ec2 = session.client("ec2")
    if not preview:
        instance = ec2.describe_instances(InstanceIds=[outputs["InstanceId"]])["Reservations"][0]["Instances"][0]
        if instance["State"]["Name"] != "stopped":
            raise RuntimeError("Stop the instance before changing its static IPs")
    template = (HERE / "stack.yaml").read_text(encoding="utf-8")
    cloudformation.validate_template(TemplateBody=template)
    params = [{"ParameterKey": name, "UsePreviousValue": True} for name in values if name not in target]
    params.extend({"ParameterKey": name, "ParameterValue": value} for name, value in target.items())
    execute_reviewed_update(
        cloudformation, stack, params, template=template, preview_only=preview,
        review=lambda changes: assert_safe_static_ip_change(changes, current, count),
    )
    if preview:
        return
    updated = cloudformation.describe_stacks(StackName=stack)["Stacks"][0]
    new_outputs = {item["OutputKey"]: item["OutputValue"] for item in updated["Outputs"]}
    addresses = [new_outputs[key] for key in ("StaticOutboundIp", "SecondaryStaticOutboundIp") if key in new_outputs]
    owned = ec2.describe_addresses(
        Filters=[{"Name": "tag:aws:cloudformation:stack-name", "Values": [stack]}]
    )["Addresses"]
    if len(addresses) != count or len(owned) != count:
        raise RuntimeError(f"Expected {count} stack-owned static IP(s), found {len(owned)}")
    if count:
        print(f"Static IPs for Dhan (primary first): {', '.join(addresses)}")
    else:
        print("All stack-owned static IPs released; register new IPs before live trading")


LIVE_CREDENTIAL_KEYS = (
    "DHAN_CLIENT_ID", "DHAN_ACCESS_TOKEN", "DHAN_MASTER_TRADING_TOKEN",
    "DHAN_CHILD_CLIENT_ID", "DHAN_CHILD_ACCESS_TOKEN", "APP_ORDER_PIN",
)


def live_credentials(path: Path) -> dict[str, str]:
    values = dotenv_values(path)
    credentials = {key: str(values.get(key) or "").strip() for key in LIVE_CREDENTIAL_KEYS}
    for key, value in credentials.items():
        if any(char in value for char in "\r\n\"'\\$`"):
            raise ValueError(f"{key} contains characters that cannot be stored safely")
    if not credentials["DHAN_CLIENT_ID"] or not credentials["DHAN_ACCESS_TOKEN"]:
        raise ValueError("The local Master Dhan credentials are missing")
    if bool(credentials["DHAN_CHILD_CLIENT_ID"]) != bool(credentials["DHAN_CHILD_ACCESS_TOKEN"]):
        raise ValueError("Set both Child Dhan values or neither")
    tokens = [credentials[key] for key in LIVE_CREDENTIAL_KEYS if key.endswith("TOKEN") and credentials[key]]
    if any(token.lower().startswith("mock") for token in tokens):
        raise ValueError("Mock tokens cannot be pushed as live credentials")
    if not re.fullmatch(r"[0-9]{4,12}", credentials["APP_ORDER_PIN"]) or credentials["APP_ORDER_PIN"] == "000000":
        raise ValueError("Set a private numeric APP_ORDER_PIN before enabling real orders")
    return {key: value for key, value in credentials.items() if value}


def live_config_command(region: str, parameter: str, dry_run: bool) -> str:
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", parameter) or not re.fullmatch(r"[a-z0-9-]+", region):
        raise ValueError("Invalid parameter coordinates")
    dry = "true" if dry_run else "false"
    return f"""set -euo pipefail
python3.11 - <<'PY'
import json, os, subprocess, tempfile
raw = subprocess.check_output(["aws", "ssm", "get-parameter", "--region", "{region}", "--name", "{parameter}",
    "--with-decryption", "--query", "Parameter.Value", "--output", "text"])
creds = json.loads(raw)
managed = {set(LIVE_CREDENTIAL_KEYS)!r} | {{"APP_DRY_RUN"}}
path = "/opt/trading/backend/.env"
st = os.stat(path)
lines = [l for l in open(path, encoding="utf-8").read().splitlines() if l.split("=", 1)[0] not in managed]
# Operator trading defaults (APP_FAST_ORDERS, TRADING_DEFAULT_ACCOUNTS, ALGO_AUTO_ARM) are preserved.
if not any(l.startswith("APP_FAST_ORDERS=") for l in lines):
    lines.append("APP_FAST_ORDERS=false")
lines += [f"{{k}}={{v}}" for k, v in creds.items()] + ["APP_DRY_RUN={dry}"]
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
with os.fdopen(fd, "w", encoding="utf-8") as f:
    f.write("\\n".join(lines) + "\\n")
os.chown(tmp, st.st_uid, st.st_gid)
os.chmod(tmp, 0o640)
os.replace(tmp, path)
print("Applied live configuration keys:", sorted(creds) + ["APP_DRY_RUN={dry}"])
PY
systemctl restart trading-dashboard
for i in $(seq 1 30); do curl -fsS http://127.0.0.1:8000/api/health && exit 0; sleep 2; done
echo "dashboard did not become healthy" >&2; exit 1
"""


def assert_safe_live_credential_grant(changes: list[dict]) -> None:
    if len(changes) != 1:
        raise RuntimeError("Expected only the instance role to change")
    resource = changes[0]["ResourceChange"]
    if (
        resource["LogicalResourceId"] != "InstanceRole" or resource["Action"] != "Modify"
        or resource.get("ResourceType") != "AWS::IAM::Role" or resource.get("Replacement") != "False"
    ):
        raise RuntimeError("Refusing an unexpected change while granting live credential access")


def push_live_credentials(profile: str, stack: str, approved: bool, dry_run: bool) -> None:
    if not approved:
        raise RuntimeError("Live credentials enable real broker access; confirm the owner approved it")
    if not STACK_NAME.fullmatch(stack):
        raise ValueError("Invalid stack name")
    credentials = live_credentials(ROOT / "backend" / ".env")
    session = boto3.Session(profile_name=profile, region_name=REGION)
    cloudformation = session.client("cloudformation")
    current = cloudformation.describe_stacks(StackName=stack)["Stacks"][0]
    outputs = {item["OutputKey"]: item["OutputValue"] for item in current["Outputs"]}
    values = {item["ParameterKey"]: item["ParameterValue"] for item in current["Parameters"]}
    if not dry_run and current_static_ip_count(values) == 0:
        raise RuntimeError("Real orders need a broker-whitelisted static IP; run set-static-ips first")
    deployed = cloudformation.get_template(StackName=stack, TemplateStage="Original")["TemplateBody"]
    if "${AWS::StackName}/dhan" not in (deployed if isinstance(deployed, str) else json.dumps(deployed)):
        if current["StackStatus"] not in UPDATABLE_STACK_STATES:
            raise RuntimeError("Stack is not in an updateable state")
        template = (HERE / "stack.yaml").read_text(encoding="utf-8")
        cloudformation.validate_template(TemplateBody=template)
        params = [{"ParameterKey": name, "UsePreviousValue": True} for name in values]
        execute_reviewed_update(
            cloudformation, stack, params, template=template, review=assert_safe_live_credential_grant
        )
        print("Instance role may now read the encrypted Dhan parameter")
    parameter = f"/{stack}/dhan"
    session.client("ssm").put_parameter(
        Name=parameter, Description="Dhan live trading credentials for the private dashboard",
        Type="SecureString", Value=json.dumps(credentials, separators=(",", ":")), Overwrite=True,
    )
    print(f"Stored {len(credentials)} live values in encrypted {parameter}")
    ssm = session.client("ssm")
    instance_id = outputs["InstanceId"]
    command_id = ssm.send_command(
        InstanceIds=[instance_id], DocumentName="AWS-RunShellScript", TimeoutSeconds=120,
        Comment="Apply live Dhan configuration (no secret values in this command)",
        Parameters={"commands": [live_config_command(REGION, parameter, dry_run)]},
    )["Command"]["CommandId"]
    waiter = ssm.get_waiter("command_executed")
    try:
        waiter.wait(CommandId=command_id, InstanceId=instance_id, WaiterConfig={"Delay": 5, "MaxAttempts": 40})
    finally:
        result = ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
        print(result.get("StandardOutputContent", "").strip())
        if result.get("Status") != "Success":
            print(result.get("StandardErrorContent", "").strip())
    print(f"Live configuration applied; dryRun={'true' if dry_run else 'false'}; "
          "fast-order/auto-arm defaults in the instance .env were preserved")


def publish_app_artifact(profile: str, stack: str, approved: bool) -> None:
    """Upload the current code as the bootstrap package for a rebuilt instance (live-safe).

    Only AppArtifactKey changes. The running app, its .env and live credentials are untouched;
    the instance must be stopped because the pointer change rewrites its user data.
    """
    if not approved:
        raise ValueError("Confirm that publishing the application package was approved")
    if not STACK_NAME.fullmatch(stack):
        raise ValueError("Invalid stack name")
    session = boto3.Session(profile_name=profile, region_name=REGION)
    current = session.client("cloudformation").describe_stacks(StackName=stack)["Stacks"][0]
    if current["StackStatus"] not in UPDATABLE_STACK_STATES:
        raise RuntimeError("Stack is not in an updateable state")
    values = {item["ParameterKey"]: item["ParameterValue"] for item in current["Parameters"]}
    outputs = {item["OutputKey"]: item["OutputValue"] for item in current["Outputs"]}
    instance = session.client("ec2").describe_instances(InstanceIds=[outputs["InstanceId"]])
    state = instance["Reservations"][0]["Instances"][0]["State"]["Name"]
    if state != "stopped":
        raise RuntimeError(f"Instance is {state}; stop it before changing the bootstrap package")
    account_id = session.client("sts").get_caller_identity()["Account"]
    s3 = session.client("s3")
    create_or_verify_bucket(s3, values["ArtifactBucket"], account_id, REGION)
    build_frontend()
    key = upload_artifact(s3, values["ArtifactBucket"], stack, "app", application_archive())
    if key == values["AppArtifactKey"]:
        print("The stack already points to this application package")
        return
    parameters = [
        {"ParameterKey": name, "ParameterValue": key}
        if name == "AppArtifactKey" else {"ParameterKey": name, "UsePreviousValue": True}
        for name in values
    ]
    execute_reviewed_update(
        session.client("cloudformation"), stack, parameters, review=assert_safe_mock_app_pointer_update
    )
    print(f"Application package published: {key}")


def prepare_mock_release(profile: str, stack: str, data_recovery: str, confirmed: bool) -> None:
    if not confirmed:
        raise ValueError("Confirm that the isolated mock release was approved")
    if not STACK_NAME.fullmatch(stack):
        raise ValueError("Invalid stack name")
    session = boto3.Session(profile_name=profile, region_name=REGION)
    values, _ = verified_mock_release_context(session, stack, data_recovery, "stopped")
    account_id = session.client("sts").get_caller_identity()["Account"]
    s3 = session.client("s3")
    create_or_verify_bucket(s3, values["ArtifactBucket"], account_id, REGION)
    build_frontend()
    key = upload_artifact(s3, values["ArtifactBucket"], stack, "app", application_archive())
    if key == values["AppArtifactKey"]:
        print("The stack already points to this immutable mock application artifact")
        return
    parameters = [
        {"ParameterKey": name, "ParameterValue": key}
        if name == "AppArtifactKey" else {"ParameterKey": name, "UsePreviousValue": True}
        for name in values
    ]
    execute_reviewed_update(
        session.client("cloudformation"), stack, parameters, review=assert_safe_mock_app_pointer_update
    )
    print("Mock app artifact pointer updated; apply the release and verify the restarted instance")


def apply_mock_release(profile: str, stack: str, data_recovery: str, confirmed: bool) -> None:
    if not confirmed:
        raise ValueError("Confirm that the isolated mock release was approved")
    if not STACK_NAME.fullmatch(stack):
        raise ValueError("Invalid stack name")
    session = boto3.Session(profile_name=profile, region_name=REGION)
    values, outputs = verified_mock_release_context(session, stack, data_recovery, "running")
    ssm = session.client("ssm")
    instance_id, volume_id = outputs["InstanceId"], outputs["DataVolumeId"]
    online = ssm.describe_instance_information(
        Filters=[{"Key": "InstanceIds", "Values": [instance_id]}]
    )["InstanceInformationList"]
    if len(online) != 1 or online[0]["PingStatus"] != "Online":
        raise RuntimeError("The mock instance is not online in Systems Manager")
    bucket, key = values["ArtifactBucket"], values["AppArtifactKey"]
    if not key.startswith(stack + "/"):
        raise RuntimeError("The application artifact belongs to a different stack")
    account_id = session.client("sts").get_caller_identity()["Account"]
    metadata = session.client("s3").head_object(
        Bucket=bucket, Key=key, ExpectedBucketOwner=account_id
    )["Metadata"]
    digest = metadata.get("sha256", "")
    command = mock_release_command(bucket, key, digest, volume_id)
    command_id = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": [command], "executionTimeout": ["600"]},
        TimeoutSeconds=600,
    )["Command"]["CommandId"]
    try:
        ssm.get_waiter("command_executed").wait(
            CommandId=command_id, InstanceId=instance_id,
            WaiterConfig={"Delay": 5, "MaxAttempts": 150},
        )
    except WaiterError:
        result = ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
        print(result.get("StandardOutputContent", ""))
        print(result.get("StandardErrorContent", ""))
        raise
    result = ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
    print(result.get("StandardOutputContent", ""))
    if result["Status"] != "Success":
        raise RuntimeError("Mock release did not complete successfully")
    print("Mock release installed from the stack's verified immutable artifact")


def update_mock_deployment(profile: str, stack: str, *, reporting_only: bool = False) -> None:
    if not STACK_NAME.fullmatch(stack):
        raise ValueError("Invalid stack name")
    session = boto3.Session(profile_name=profile, region_name=REGION)
    cloudformation = session.client("cloudformation")
    current = cloudformation.describe_stacks(StackName=stack)["Stacks"][0]
    if current["StackStatus"] not in UPDATABLE_STACK_STATES:
        raise RuntimeError("Stack is not in an updateable state")
    if {tag["Key"]: tag["Value"] for tag in current.get("Tags", [])}.get("DeploymentMode") != "mock":
        raise RuntimeError("Automatic artifact updates are restricted to the mock deployment")
    values = {item["ParameterKey"]: item["ParameterValue"] for item in current["Parameters"]}
    if values["AutomationEnabled"] != "false":
        raise RuntimeError("Disable automatic start and stop before updating mock artifacts")
    if not reporting_only and values.get("DataAttachmentEnabled", "true") != "true":
        raise RuntimeError("Reattach the data volume before updating instance artifacts")
    instance_id = cloudformation.describe_stack_resource(
        StackName=stack, LogicalResourceId="DashboardInstance"
    )["StackResourceDetail"]["PhysicalResourceId"]
    instance = session.client("ec2").describe_instances(InstanceIds=[instance_id])
    instance_details = instance["Reservations"][0]["Instances"][0]
    state = instance_details["State"]["Name"]
    if not reporting_only and state != "stopped":
        raise RuntimeError("Stop the mock instance before updating deployment artifacts")

    account_id = session.client("sts").get_caller_identity()["Account"]
    bucket = values["ArtifactBucket"]
    create_or_verify_bucket(session.client("s3"), bucket, account_id, REGION)
    create_or_verify_secret(session.client("ssm"), values["TelegramParameter"], telegram_credentials(ROOT / "backend" / ".env"))
    s3 = session.client("s3")
    updates = {
        "LambdaArtifactKey": upload_artifact(s3, bucket, stack, "lambda", lambda_archive()),
    }
    if not reporting_only:
        build_frontend()
        updates.update(
            {
                "AppArtifactKey": upload_artifact(s3, bucket, stack, "app", application_archive()),
                "BootstrapArtifactKey": upload_artifact(s3, bucket, stack, "bootstrap", bootstrap_artifact()),
            }
        )
    parameters = [
        {"ParameterKey": key, "ParameterValue": updates[key]}
        if key in updates else {"ParameterKey": key, "UsePreviousValue": True}
        for key in values
    ]
    if "CostReportsEnabled" not in values:
        parameters.append({"ParameterKey": "CostReportsEnabled", "ParameterValue": "false"})
    template = (HERE / "stack.yaml").read_text(encoding="utf-8")
    cloudformation.validate_template(TemplateBody=template)
    execute_reviewed_update(
        cloudformation, stack, parameters, template=template,
        ebs_backed_instance=instance_details["RootDeviceType"] == "ebs",
        forbid_instance_change=reporting_only,
    )
    if reporting_only:
        print("Reporting infrastructure updated without modifying the instance")
    else:
        print("Mock stack updated; instance bootstrap still requires explicit recovery and verification")


def enable_automation(profile: str, stack: str, confirmed: bool) -> None:
    if not confirmed:
        raise ValueError("Confirm that private access, mock mode, alerts and backup restore were verified")
    cloudformation = boto3.Session(profile_name=profile, region_name=REGION).client("cloudformation")
    current = cloudformation.describe_stacks(StackName=stack)["Stacks"][0]
    if current["StackStatus"] not in UPDATABLE_STACK_STATES:
        raise RuntimeError("Stack is not in an updateable state")
    values = {param["ParameterKey"]: param["ParameterValue"] for param in current["Parameters"]}
    if values.get("DataAttachmentEnabled", "true") != "true":
        raise RuntimeError("Reattach the data volume before enabling automation")
    params = []
    for param in current["Parameters"]:
        if param["ParameterKey"] == "AutomationEnabled":
            params.append({"ParameterKey": "AutomationEnabled", "ParameterValue": "true"})
        else:
            params.append({"ParameterKey": param["ParameterKey"], "UsePreviousValue": True})
    execute_reviewed_update(cloudformation, stack, params, forbid_instance_change=True)
    print("Weekday schedules and independent health monitoring enabled")


def enable_cost_report(profile: str, stack: str, confirmed: bool) -> None:
    if not confirmed:
        raise ValueError("Confirm that the AWS cost-report test was received and inspected")
    cloudformation = boto3.Session(profile_name=profile, region_name=REGION).client("cloudformation")
    current = cloudformation.describe_stacks(StackName=stack)["Stacks"][0]
    if current["StackStatus"] not in UPDATABLE_STACK_STATES:
        raise RuntimeError("Stack is not in an updateable state")
    if not any(param["ParameterKey"] == "CostReportsEnabled" for param in current["Parameters"]):
        raise RuntimeError("The stack does not yet include the daily cost-report infrastructure")
    params = [
        {"ParameterKey": "CostReportsEnabled", "ParameterValue": "true"}
        if param["ParameterKey"] == "CostReportsEnabled"
        else {"ParameterKey": param["ParameterKey"], "UsePreviousValue": True}
        for param in current["Parameters"]
    ]
    execute_reviewed_update(cloudformation, stack, params, forbid_instance_change=True)
    print("Weekday 15:55 IST AWS usage report enabled")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="trading-deploy")
    parser.add_argument("--stack", default="trading-dashboard")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("create")
    update = subparsers.add_parser("update-mock")
    update.add_argument(
        "--reporting-only", action="store_true",
        help="Update reporting without changing instance user data or restarting the instance",
    )
    lambda_update = subparsers.add_parser("update-mock-lambda")
    lambda_update.add_argument("--approved-mock-lambda-update", action="store_true")
    subparsers.add_parser("remove-recovery-stop")
    operator = subparsers.add_parser("setup-mock-operator")
    operator.add_argument("--approved-operator-setup", action="store_true")
    subparsers.add_parser("pause-automation")
    static_ip = subparsers.add_parser("release-static-ip")
    static_ip.add_argument("--approved-static-ip-release", action="store_true")
    static_ips = subparsers.add_parser("set-static-ips")
    static_ips.add_argument("--count", type=int, choices=(0, 1, 2), required=True)
    static_ips.add_argument("--preview", action="store_true", help="Review the AWS change set without applying it")
    static_ips.add_argument("--approved-static-ip-change", action="store_true")
    live = subparsers.add_parser("push-live-credentials")
    live.add_argument("--approved-live-trading", action="store_true")
    live.add_argument("--keep-dry-run", action="store_true", help="Use real data but simulate orders")
    publish = subparsers.add_parser("publish-app-artifact")
    publish.add_argument("--approved-app-publish", action="store_true")
    detach = subparsers.add_parser("detach-mock-data")
    detach.add_argument("--data-recovery-point", required=True)
    detach.add_argument("--root-recovery-point", required=True)
    detach.add_argument("--approved-mock-replacement", action="store_true")
    replace = subparsers.add_parser("replace-mock-server")
    replace.add_argument("--data-recovery-point", required=True)
    replace.add_argument("--root-recovery-point", required=True)
    replace.add_argument("--approved-mock-replacement", action="store_true")
    release = subparsers.add_parser("apply-mock-release")
    release.add_argument("--data-recovery-point", required=True)
    release.add_argument("--approved-mock-release", action="store_true")
    prepare = subparsers.add_parser("prepare-mock-release")
    prepare.add_argument("--data-recovery-point", required=True)
    prepare.add_argument("--approved-mock-release", action="store_true")
    enable = subparsers.add_parser("enable-automation")
    enable.add_argument("--verified-private-mock-alerts-and-restore", action="store_true")
    cost = subparsers.add_parser("enable-cost-report")
    cost.add_argument("--verified-cost-report", action="store_true")
    args = parser.parse_args()
    if args.command == "create":
        create_deployment(args.profile, args.stack)
    elif args.command == "update-mock":
        update_mock_deployment(args.profile, args.stack, reporting_only=args.reporting_only)
    elif args.command == "update-mock-lambda":
        update_mock_lambda_artifact(args.profile, args.stack, args.approved_mock_lambda_update)
    elif args.command == "remove-recovery-stop":
        remove_recovery_stop(args.profile, args.stack)
    elif args.command == "setup-mock-operator":
        setup_mock_operator(args.profile, args.stack, args.approved_operator_setup)
    elif args.command == "pause-automation":
        pause_automation(args.profile, args.stack)
    elif args.command == "release-static-ip":
        release_static_ip(args.profile, args.stack, args.approved_static_ip_release)
    elif args.command == "set-static-ips":
        set_static_ips(args.profile, args.stack, args.count, args.approved_static_ip_change, args.preview)
    elif args.command == "push-live-credentials":
        push_live_credentials(args.profile, args.stack, args.approved_live_trading, args.keep_dry_run)
    elif args.command == "publish-app-artifact":
        publish_app_artifact(args.profile, args.stack, args.approved_app_publish)
    elif args.command == "detach-mock-data":
        detach_mock_data(
            args.profile, args.stack, args.data_recovery_point, args.root_recovery_point,
            args.approved_mock_replacement,
        )
    elif args.command == "replace-mock-server":
        replace_mock_server(
            args.profile, args.stack, args.data_recovery_point, args.root_recovery_point,
            args.approved_mock_replacement,
        )
    elif args.command == "apply-mock-release":
        apply_mock_release(args.profile, args.stack, args.data_recovery_point, args.approved_mock_release)
    elif args.command == "prepare-mock-release":
        prepare_mock_release(args.profile, args.stack, args.data_recovery_point, args.approved_mock_release)
    elif args.command == "enable-automation":
        enable_automation(args.profile, args.stack, args.verified_private_mock_alerts_and_restore)
    else:
        enable_cost_report(args.profile, args.stack, args.verified_cost_report)


if __name__ == "__main__":
    main()
