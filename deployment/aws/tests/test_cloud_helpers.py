import importlib.util
import json
import tempfile
import unittest
import zipfile
from datetime import datetime
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from zoneinfo import ZoneInfo

from botocore.exceptions import ClientError
from botocore.stub import ANY, Stubber


HERE = Path(__file__).resolve().parents[1]


def load_module(name):
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


alerts = load_module("alerts")
monitor = load_module("monitor")
write_config = load_module("write_config")
deploy = load_module("deploy")


class CloudHelpersTest(unittest.TestCase):
    def test_schedule_windows_and_weekends(self):
        tz = ZoneInfo("Asia/Kolkata")
        cases = [
            (datetime(2026, 11, 2, 8, 54, tzinfo=tz), "stopped"),
            (datetime(2026, 11, 2, 8, 55, tzinfo=tz), "grace"),
            (datetime(2026, 11, 2, 9, 9, tzinfo=tz), "grace"),
            (datetime(2026, 11, 2, 9, 10, tzinfo=tz), "running"),
            (datetime(2026, 11, 2, 15, 59, tzinfo=tz), "running"),
            (datetime(2026, 11, 2, 16, 0, tzinfo=tz), "grace"),
            (datetime(2026, 11, 2, 16, 14, tzinfo=tz), "grace"),
            (datetime(2026, 11, 2, 16, 15, tzinfo=tz), "stopped"),
            (datetime(2026, 11, 7, 12, 0, tzinfo=tz), "stopped"),
        ]
        for now, expected in cases:
            with self.subTest(now=now):
                self.assertEqual(monitor.expected_state(now), expected)

    def test_sns_messages_do_not_forward_raw_payload(self):
        text = alerts.notification_text(
            'ALARM: "trading-availability" in Asia Pacific',
            "private account data and token-like-secret",
        )
        self.assertIn("dashboard is unavailable", text)
        self.assertNotIn("private", text)
        self.assertNotIn("secret", text)
        self.assertIn("budget", alerts.notification_text("AWS Budget Notification", "private").lower())
        test = alerts.notification_text("TEST: trading-dashboard-alerts", "private")
        self.assertIn("AWS TEST", test)
        self.assertNotIn("private", test)
        backup = json.dumps({"detail-type": "Backup Job State Change", "detail": {"secret": "private"}})
        self.assertIn("backup failed", alerts.notification_text("", backup).lower())
        self.assertNotIn("private", alerts.notification_text("", backup))

    def test_mock_config_is_isolated_and_does_not_replace_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            secret = Path(directory) / "secret.json"
            destination = Path(directory) / ".env"
            secret.write_text(json.dumps({"token": "123:abc", "chat_id": "-100123"}), encoding="utf-8")
            write_config.create_config(secret, destination)
            content = destination.read_text(encoding="utf-8")
            self.assertIn("DHAN_ACCESS_TOKEN=mock-token\n", content)
            self.assertIn("APP_DRY_RUN=true\n", content)
            self.assertIn("APP_FAST_ORDERS=false\n", content)
            self.assertIn("TELEGRAM_ENABLED=true\n", content)
            with self.assertRaises(FileExistsError):
                write_config.create_config(secret, destination)

    def test_config_rejects_newline_injection_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            secret = Path(directory) / "secret.json"
            destination = Path(directory) / ".env"
            secret.write_text(json.dumps({"token": "123:abc\nAPP_DRY_RUN=false", "chat_id": "-100123"}))
            with self.assertRaises(ValueError):
                write_config.create_config(secret, destination)
            self.assertFalse(destination.exists())

    def test_grace_does_not_query_ec2(self):
        with patch.object(monitor, "dashboard_healthy", side_effect=AssertionError):
            now = datetime(2026, 11, 2, 9, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
            self.assertTrue(monitor.availability(None, None, "i-example", now))

    def test_running_host_requires_all_local_services_and_health(self):
        ec2 = Mock()
        ec2.describe_instances.return_value = {
            "Reservations": [{"Instances": [{"State": {"Name": "running"}}]}]
        }
        ssm = Mock()
        ssm.send_command.return_value = {"Command": {"CommandId": "command"}}
        ssm.get_command_invocation.return_value = {"Status": "Success"}
        now = datetime(2026, 11, 2, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
        self.assertTrue(monitor.availability(ec2, ssm, "i-example", now))
        commands = ssm.send_command.call_args.kwargs["Parameters"]["commands"]
        self.assertTrue(any("trading-dashboard.service" in command for command in commands))
        self.assertTrue(any("amazon-cloudwatch-agent.service" in command for command in commands))
        self.assertTrue(any("/api/health" in command for command in commands))
        ssm.get_command_invocation.return_value = {"Status": "Failed"}
        self.assertFalse(monitor.availability(ec2, ssm, "i-example", now))

    def test_health_check_uses_valid_ssm_delivery_timeout(self):
        ssm = monitor.boto3.client(
            "ssm", region_name="ap-south-1", aws_access_key_id="test", aws_secret_access_key="test"
        )
        instance_id = "i-0123456789abcdef0"
        command_id = "00000000-0000-0000-0000-000000000000"
        with Stubber(ssm) as stub:
            stub.add_response(
                "send_command",
                {"Command": {"CommandId": command_id}},
                {
                    "InstanceIds": [instance_id],
                    "DocumentName": "AWS-RunShellScript",
                    "TimeoutSeconds": 30,
                    "Parameters": {"commands": ANY, "executionTimeout": ["15"]},
                },
            )
            stub.add_response(
                "get_command_invocation",
                {"Status": "Success"},
                {"CommandId": command_id, "InstanceId": instance_id},
            )
            self.assertTrue(monitor.dashboard_healthy(ssm, instance_id))
            stub.assert_no_pending_responses()

    def test_unreachable_host_is_unhealthy_but_access_denied_is_not_hidden(self):
        ssm = Mock()
        ssm.send_command.side_effect = ClientError(
            {"Error": {"Code": "InvalidInstanceId", "Message": "offline"}}, "SendCommand"
        )
        self.assertFalse(monitor.dashboard_healthy(ssm, "i-example"))
        ssm.send_command.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "denied"}}, "SendCommand"
        )
        with self.assertRaises(ClientError):
            monitor.dashboard_healthy(ssm, "i-example")

    def test_stopped_schedule_rejects_running_instance(self):
        ec2 = Mock()
        ec2.describe_instances.return_value = {
            "Reservations": [{"Instances": [{"State": {"Name": "running"}}]}]
        }
        now = datetime(2026, 11, 2, 16, 15, tzinfo=ZoneInfo("Asia/Kolkata"))
        self.assertFalse(monitor.availability(ec2, None, "i-example", now))

    def test_telegram_http_error_does_not_expose_token(self):
        token = "123:private_token"
        error = HTTPError(f"https://api.telegram.org/bot{token}/sendMessage", 429, "rate", {}, None)
        with patch.object(alerts, "urlopen", side_effect=error):
            with self.assertRaises(RuntimeError) as raised:
                alerts.send_message(token, "-100123", "test")
        self.assertIn("429", str(raised.exception))
        self.assertNotIn(token, str(raised.exception))

    def test_artifacts_are_deterministic_and_reject_unsafe_paths(self):
        files = {"app/safe.py": b"safe", "app/config.txt": b"no secrets"}
        first = deploy.create_archive(files)
        self.assertEqual(first, deploy.create_archive(files))
        with zipfile.ZipFile(BytesIO(first)) as archive:
            self.assertEqual(set(archive.namelist()), set(files))
            self.assertEqual(archive.getinfo("app/safe.py").external_attr >> 16, 0o100644)
        for name in ("../.env", "/root/.env", "app\\..\\secret"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                deploy.create_archive({name: b"bad"})

    def test_mock_release_command_requires_verified_artifact_coordinates(self):
        digest = "a" * 64
        key = f"trading-dashboard/app-{digest[:20]}"
        command = deploy.mock_release_command("private-artifacts", key, digest, "vol-123abc")
        self.assertIn("bash -s -- 'private-artifacts'", command)
        self.assertIn("TRADING_MOCK_RELEASE_SCRIPT", command)
        self.assertIn("verify_mock_state", command)
        invalid = (
            ("private';echo-bad", key, digest, "vol-123abc"),
            ("private-artifacts", "../app-" + digest[:20], digest, "vol-123abc"),
            ("private-artifacts", key, "b" * 64, "vol-123abc"),
            ("private-artifacts", key, digest, "vol-123;bad"),
        )
        for args in invalid:
            with self.subTest(args=args), self.assertRaises(ValueError):
                deploy.mock_release_command(*args)

    def test_mock_release_requires_explicit_approval(self):
        with patch.object(deploy.boto3, "Session") as session:
            with self.assertRaises(ValueError):
                deploy.apply_mock_release("test", "trading-dashboard", "recovery", False)
            with self.assertRaises(ValueError):
                deploy.prepare_mock_release("test", "trading-dashboard", "recovery", False)
        session.assert_not_called()

    def test_app_pointer_review_rejects_unrelated_or_unexpected_changes(self):
        instance = {"ResourceChange": {
            "LogicalResourceId": "DashboardInstance", "Action": "Modify", "Replacement": "Conditional",
            "Details": [
                {"Target": {"Name": "UserData", "RequiresRecreation": "Conditionally"},
                 "ChangeSource": "ParameterReference", "CausingEntity": "AppArtifactKey"},
            ],
        }}
        attachment = {"ResourceChange": {
            "LogicalResourceId": "DataAttachment", "Action": "Modify", "Replacement": "Conditional",
            "Details": [
                {"Target": {"Name": "InstanceId"}, "ChangeSource": "ResourceReference",
                 "CausingEntity": "DashboardInstance"},
            ],
        }}
        deploy.assert_safe_mock_app_pointer_update([instance, attachment])
        unrelated = {"ResourceChange": {
            "LogicalResourceId": "DataVolume", "Action": "Modify", "Replacement": "False",
            "Details": [{"Target": {"Name": "Size"}, "ChangeSource": "DirectModification"}],
        }}
        with self.assertRaises(RuntimeError):
            deploy.assert_safe_mock_app_pointer_update([instance, attachment, unrelated])
        unexpected = {"ResourceChange": {
            "LogicalResourceId": "AvailabilityAlarm", "Action": "Modify", "Replacement": "False",
            "Details": [{"Target": {"Name": "Threshold"}, "ChangeSource": "DirectModification"}],
        }}
        with self.assertRaises(RuntimeError):
            deploy.assert_safe_mock_app_pointer_update([instance, unexpected])

    def test_mock_release_requires_matching_stop_backup_and_private_instance(self):
        digest = "a" * 64
        stop_at = "at(2026-11-02T16:00:00)"
        cfn, ec2, backup, ssm, scheduler, s3, sts = (Mock() for _ in range(7))
        cfn.describe_stacks.return_value = {"Stacks": [{
            "StackStatus": "UPDATE_COMPLETE",
            "Tags": [{"Key": "DeploymentMode", "Value": "mock"}],
            "Parameters": [
                {"ParameterKey": "AutomationEnabled", "ParameterValue": "false"},
                {"ParameterKey": "DataAttachmentEnabled", "ParameterValue": "true"},
                {"ParameterKey": "RecoveryStopAt", "ParameterValue": stop_at},
                {"ParameterKey": "ArtifactBucket", "ParameterValue": "private-artifacts"},
                {"ParameterKey": "AppArtifactKey", "ParameterValue": f"trading-dashboard/app-{digest[:20]}"},
            ],
            "Outputs": [
                {"OutputKey": "InstanceId", "OutputValue": "i-123"},
                {"OutputKey": "DataVolumeId", "OutputValue": "vol-123"},
                {"OutputKey": "BackupVaultName", "OutputValue": "vault"},
            ],
        }]}
        ec2.describe_instances.return_value = {"Reservations": [{"Instances": [{
            "State": {"Name": "running"}, "RootDeviceType": "ebs",
            "SecurityGroups": [{"GroupId": "sg-123"}],
        }]}]}
        ec2.describe_security_groups.return_value = {"SecurityGroups": [{"IpPermissions": []}]}
        ec2.describe_volumes.return_value = {"Volumes": [{
            "Encrypted": True, "Attachments": [{"InstanceId": "i-123", "State": "attached"}],
        }]}
        backup.describe_recovery_point.return_value = {
            "Status": "COMPLETED", "IsEncrypted": True, "ResourceArn": "arn:aws:ec2:::volume/vol-123",
        }
        ssm.describe_instance_information.return_value = {"InstanceInformationList": [{"PingStatus": "Online"}]}
        ssm.send_command.return_value = {"Command": {"CommandId": "command"}}
        ssm.get_command_invocation.return_value = {"Status": "Success", "StandardOutputContent": ""}
        scheduler.get_schedule.return_value = {
            "State": "ENABLED", "ScheduleExpression": stop_at,
            "ScheduleExpressionTimezone": "Asia/Kolkata", "FlexibleTimeWindow": {"Mode": "OFF"},
            "Target": {
                "Arn": "arn:aws:scheduler:::aws-sdk:ec2:stopInstances",
                "Input": '{"InstanceIds":["i-123"]}',
            },
        }
        sts.get_caller_identity.return_value = {"Account": "123456789012"}
        s3.head_object.return_value = {"Metadata": {"sha256": digest}}
        clients = {
            "cloudformation": cfn, "ec2": ec2, "backup": backup, "ssm": ssm,
            "scheduler": scheduler, "s3": s3, "sts": sts,
        }
        session = Mock()
        session.client.side_effect = clients.__getitem__
        with patch.object(deploy.boto3, "Session", return_value=session), \
                patch.object(deploy, "recovery_stop_expression", return_value=stop_at):
            scheduler.get_schedule.return_value["Target"]["Input"] = '{"InstanceIds":["i-other"]}'
            with self.assertRaisesRegex(RuntimeError, "16:00 safety stop"):
                deploy.apply_mock_release("test", "trading-dashboard", "recovery", True)
            ssm.send_command.assert_not_called()
            scheduler.get_schedule.return_value["Target"]["Input"] = '{"InstanceIds":["i-123"]}'
            deploy.apply_mock_release("test", "trading-dashboard", "recovery", True)
        ssm.send_command.assert_called_once()

    def test_lambda_artifact_includes_iana_timezone_data(self):
        with zipfile.ZipFile(BytesIO(deploy.lambda_archive())) as archive:
            self.assertIn("monitor.py", archive.namelist())
            self.assertIn("alerts.py", archive.namelist())
            self.assertIn("cost_report.py", archive.namelist())
            self.assertIn("tzdata/zoneinfo/Asia/Kolkata", archive.namelist())

    def test_lambda_pointer_review_allows_only_function_codes_and_expected_dependents(self):
        def change(name, target="Code", source="ParameterReference", cause="LambdaArtifactKey"):
            return {"ResourceChange": {
                "LogicalResourceId": name, "Action": "Modify", "Replacement": "False",
                "ResourceType": "AWS::Lambda::Function",
                "Details": [{"Target": {"Name": target}, "ChangeSource": source, "CausingEntity": cause}],
            }}

        valid = [change(name) for name in ("AlertFunction", "MonitorFunction", "CostReportFunction")]
        dependent = {"ResourceChange": {
            "LogicalResourceId": "MonitorSchedule", "Action": "Modify", "Replacement": "False",
            "ResourceType": "AWS::Events::Rule",
            "Details": [{
                "Target": {"Name": "Targets"}, "ChangeSource": "ResourceAttribute",
                "CausingEntity": "MonitorFunction.Arn",
            }],
        }}
        deploy.assert_safe_mock_lambda_pointer_update(valid + [dependent])
        unsafe = (
            valid[:-1],
            valid + [change("DashboardInstance")],
            valid + [change("MonitorFunction")],
            [change("AlertFunction", target="Environment")] + valid[1:],
            [change("AlertFunction", source="DirectModification", cause=None)] + valid[1:],
            valid + [{"ResourceChange": dict(dependent["ResourceChange"], Replacement="True")}],
        )
        for changes in unsafe:
            with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                deploy.assert_safe_mock_lambda_pointer_update(changes)

    def test_lambda_update_keeps_enabled_automation_and_instance_parameters(self):
        cloudformation = Mock()
        cloudformation.describe_stacks.return_value = {"Stacks": [{
            "StackStatus": "UPDATE_COMPLETE",
            "Tags": [{"Key": "DeploymentMode", "Value": "mock"}],
            "Parameters": [
                {"ParameterKey": "AutomationEnabled", "ParameterValue": "true"},
                {"ParameterKey": "ArtifactBucket", "ParameterValue": "private-bucket"},
                {"ParameterKey": "AppArtifactKey", "ParameterValue": "old-app"},
                {"ParameterKey": "LambdaArtifactKey", "ParameterValue": "old-lambda"},
            ],
        }]}
        sts = Mock()
        sts.get_caller_identity.return_value = {"Account": "123456789012"}
        session = Mock()
        session.client.side_effect = lambda name: {"cloudformation": cloudformation, "sts": sts}.get(name, Mock())
        with (
            patch.object(deploy.boto3, "Session", return_value=session),
            patch.object(deploy, "create_or_verify_bucket"),
            patch.object(deploy, "lambda_archive", return_value=b"lambda"),
            patch.object(deploy, "upload_artifact", return_value="new-lambda"),
            patch.object(deploy, "execute_reviewed_update") as execute,
        ):
            deploy.update_mock_lambda_artifact("profile", "trading-dashboard", True)
        params = execute.call_args.args[2]
        self.assertIn({"ParameterKey": "AutomationEnabled", "UsePreviousValue": True}, params)
        self.assertIn({"ParameterKey": "AppArtifactKey", "UsePreviousValue": True}, params)
        self.assertIn({"ParameterKey": "LambdaArtifactKey", "ParameterValue": "new-lambda"}, params)
        self.assertIs(execute.call_args.kwargs["review"], deploy.assert_safe_mock_lambda_pointer_update)

    def test_recovery_stop_removal_review_refuses_all_other_changes(self):
        stop = {"ResourceChange": {
            "Action": "Remove", "LogicalResourceId": "RecoverySafetyStop",
            "ResourceType": "AWS::Scheduler::Schedule",
        }}
        deploy.assert_safe_recovery_stop_removal([stop])
        for changes in ([], [stop, stop], [{"ResourceChange": dict(
            stop["ResourceChange"], LogicalResourceId="WeekdayStop"
        )}]):
            with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                deploy.assert_safe_recovery_stop_removal(changes)

    def test_recovery_stop_removal_requires_correct_recurring_target(self):
        cloudformation = Mock()
        cloudformation.describe_stacks.return_value = {"Stacks": [{
            "StackStatus": "UPDATE_COMPLETE",
            "Tags": [{"Key": "DeploymentMode", "Value": "mock"}],
            "Parameters": [
                {"ParameterKey": "AutomationEnabled", "ParameterValue": "true"},
                {"ParameterKey": "RecoveryStopAt", "ParameterValue": "at(2026-11-02T16:00:00)"},
            ],
            "Outputs": [{"OutputKey": "InstanceId", "OutputValue": "i-example"}],
        }]}
        scheduler = Mock()
        scheduler.get_schedule.return_value = {
            "State": "ENABLED", "ScheduleExpression": "cron(0 16 ? * MON-FRI *)",
            "ScheduleExpressionTimezone": "Asia/Kolkata", "FlexibleTimeWindow": {"Mode": "OFF"},
            "Target": {
                "Arn": "arn:aws:scheduler:::aws-sdk:ec2:stopInstances",
                "Input": '{"InstanceIds":["i-other"]}',
            },
        }
        session = Mock()
        session.client.side_effect = {"cloudformation": cloudformation, "scheduler": scheduler}.__getitem__
        morning = datetime(2026, 11, 2, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
        with (
            patch.object(deploy.boto3, "Session", return_value=session),
            patch.object(deploy, "datetime", wraps=datetime) as clock,
            patch.object(deploy, "execute_reviewed_update") as execute,
        ):
            clock.now.return_value = morning
            with self.assertRaisesRegex(RuntimeError, "16:00 stop"):
                deploy.remove_recovery_stop("profile", "trading-dashboard")
            execute.assert_not_called()
            scheduler.get_schedule.return_value["Target"]["Input"] = '{"InstanceIds":["i-example"]}'
            deploy.remove_recovery_stop("profile", "trading-dashboard")
        params = execute.call_args.args[2]
        self.assertIn({"ParameterKey": "RecoveryStopAt", "ParameterValue": ""}, params)
        self.assertIn({"ParameterKey": "AutomationEnabled", "UsePreviousValue": True}, params)
        self.assertIs(execute.call_args.kwargs["review"], deploy.assert_safe_recovery_stop_removal)

    def test_operator_setup_review_refuses_unrelated_or_replacement_changes(self):
        user = {"ResourceChange": {
            "Action": "Add", "LogicalResourceId": "OperatorUser", "ResourceType": "AWS::IAM::User",
        }}
        policy = {"ResourceChange": {
            "Action": "Add", "LogicalResourceId": "OperatorUserPolicy", "ResourceType": "AWS::IAM::Policy",
        }}
        role = {"ResourceChange": {
            "Action": "Modify", "LogicalResourceId": "OperatorRole", "ResourceType": "AWS::IAM::Role",
            "Replacement": "False", "Details": [
                {"Target": {"Name": "AssumeRolePolicyDocument"}, "ChangeSource": "DirectModification"},
                {"Target": {"Name": "Policies"}, "ChangeSource": "DirectModification"},
            ],
        }}
        deploy.assert_safe_mock_operator_update([user, policy, role])
        unsafe = (
            [user, role],
            [user, policy, role, {"ResourceChange": {
                "Action": "Modify", "LogicalResourceId": "WeekdayStop", "ResourceType": "AWS::Scheduler::Schedule"
            }}],
            [user, policy, {"ResourceChange": dict(role["ResourceChange"], Replacement="True")}],
            [user, policy, {"ResourceChange": dict(role["ResourceChange"], Details=[
                {"Target": {"Name": "RoleName"}, "ChangeSource": "DirectModification"}
            ])}],
        )
        for changes in unsafe:
            with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                deploy.assert_safe_mock_operator_update(changes)

    def test_pause_review_preserves_cost_reporting_and_other_resources(self):
        def change(name, resource_type, target):
            return {"ResourceChange": {
                "Action": "Modify", "LogicalResourceId": name, "ResourceType": resource_type,
                "Replacement": "False",
                "Details": [{"Target": {"Name": target}, "ChangeSource": "DirectModification"}],
            }}

        valid = [
            change("AvailabilityAlarm", "AWS::CloudWatch::Alarm", "ActionsEnabled"),
            change("MonitorSchedule", "AWS::Events::Rule", "State"),
            change("WeekdayStart", "AWS::Scheduler::Schedule", "State"),
            change("WeekdayStop", "AWS::Scheduler::Schedule", "State"),
        ]
        deploy.assert_safe_pause_automation(valid)
        for changes in (
            valid[:-1],
            valid + [change("WeekdayCostReport", "AWS::Scheduler::Schedule", "State")],
            valid + [change("DashboardInstance", "AWS::EC2::Instance", "InstanceType")],
            [change("AvailabilityAlarm", "AWS::CloudWatch::Alarm", "Threshold")] + valid[1:],
        ):
            with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                deploy.assert_safe_pause_automation(changes)

    def test_pause_requires_stopped_instance_and_preserves_reporting_parameter(self):
        cloudformation = Mock()
        cloudformation.describe_stacks.return_value = {"Stacks": [{
            "StackStatus": "UPDATE_COMPLETE",
            "Tags": [{"Key": "DeploymentMode", "Value": "mock"}],
            "Parameters": [
                {"ParameterKey": "AutomationEnabled", "ParameterValue": "true"},
                {"ParameterKey": "CostReportsEnabled", "ParameterValue": "true"},
            ],
            "Outputs": [{"OutputKey": "InstanceId", "OutputValue": "i-example"}],
        }]}
        ec2 = Mock()
        ec2.describe_instances.return_value = {
            "Reservations": [{"Instances": [{"State": {"Name": "running"}}]}]
        }
        scheduler = Mock()
        scheduler.get_schedule.return_value = {"State": "DISABLED"}
        events = Mock()
        events.describe_rule.return_value = {"State": "DISABLED"}
        cloudwatch = Mock()
        cloudwatch.describe_alarms.return_value = {"MetricAlarms": [{"ActionsEnabled": False}]}
        session = Mock()
        session.client.side_effect = {
            "cloudformation": cloudformation, "ec2": ec2, "scheduler": scheduler,
            "events": events, "cloudwatch": cloudwatch,
        }.__getitem__
        with (
            patch.object(deploy.boto3, "Session", return_value=session),
            patch.object(deploy, "execute_reviewed_update") as execute,
        ):
            with self.assertRaisesRegex(RuntimeError, "Stop the mock instance"):
                deploy.pause_automation("profile", "trading-dashboard")
            execute.assert_not_called()
            ec2.describe_instances.return_value["Reservations"][0]["Instances"][0]["State"]["Name"] = "stopped"
            deploy.pause_automation("profile", "trading-dashboard")
        params = execute.call_args.args[2]
        self.assertIn({"ParameterKey": "AutomationEnabled", "ParameterValue": "false"}, params)
        self.assertIn({"ParameterKey": "CostReportsEnabled", "UsePreviousValue": True}, params)
        self.assertIs(execute.call_args.kwargs["review"], deploy.assert_safe_pause_automation)

    def test_pause_verification_rejects_an_enabled_start(self):
        scheduler = Mock()
        scheduler.get_schedule.return_value = {"State": "ENABLED"}
        session = Mock()
        session.client.return_value = scheduler
        with self.assertRaisesRegex(RuntimeError, "weekday-start"):
            deploy.verify_paused_automation(session, "trading-dashboard")

    def test_frontend_entry_disables_stale_conditional_caching(self):
        config = (HERE / "nginx.conf").read_text(encoding="utf-8")
        self.assertIn("location = /index.html", config)
        self.assertIn('add_header Cache-Control "no-store" always;', config)
        self.assertIn("etag off;", config)
        self.assertIn("if_modified_since off;", config)

    def test_reporting_only_update_preserves_instance_artifacts(self):
        cloudformation = Mock()
        cloudformation.describe_stacks.return_value = {"Stacks": [{
            "StackStatus": "UPDATE_ROLLBACK_COMPLETE",
            "Tags": [{"Key": "DeploymentMode", "Value": "mock"}],
            "Parameters": [
                {"ParameterKey": "AutomationEnabled", "ParameterValue": "false"},
                {"ParameterKey": "ArtifactBucket", "ParameterValue": "private-bucket"},
                {"ParameterKey": "TelegramParameter", "ParameterValue": "/telegram"},
                {"ParameterKey": "AppArtifactKey", "ParameterValue": "old-app"},
                {"ParameterKey": "BootstrapArtifactKey", "ParameterValue": "old-bootstrap"},
                {"ParameterKey": "LambdaArtifactKey", "ParameterValue": "old-lambda"},
            ],
        }]}
        cloudformation.describe_stack_resource.return_value = {
            "StackResourceDetail": {"PhysicalResourceId": "i-example"}
        }
        ec2 = Mock()
        ec2.describe_instances.return_value = {
            "Reservations": [{"Instances": [{"State": {"Name": "running"}, "RootDeviceType": "ebs"}]}]
        }
        sts = Mock()
        sts.get_caller_identity.return_value = {"Account": "123456789012"}
        session = Mock()
        session.client.side_effect = lambda name: {"cloudformation": cloudformation, "ec2": ec2, "sts": sts}.get(name, Mock())
        with (
            patch.object(deploy.boto3, "Session", return_value=session),
            patch.object(deploy, "create_or_verify_bucket"),
            patch.object(deploy, "create_or_verify_secret"),
            patch.object(deploy, "telegram_credentials", return_value={}),
            patch.object(deploy, "build_frontend", side_effect=AssertionError("frontend must not be rebuilt")),
            patch.object(deploy, "lambda_archive", return_value=b"lambda"),
            patch.object(deploy, "upload_artifact", return_value="new-lambda"),
            patch.object(deploy, "execute_reviewed_update") as execute,
        ):
            deploy.update_mock_deployment("profile", "trading-dashboard", reporting_only=True)
        params = execute.call_args.args[2]
        self.assertIn({"ParameterKey": "AppArtifactKey", "UsePreviousValue": True}, params)
        self.assertIn({"ParameterKey": "BootstrapArtifactKey", "UsePreviousValue": True}, params)
        self.assertIn({"ParameterKey": "LambdaArtifactKey", "ParameterValue": "new-lambda"}, params)
        self.assertIn({"ParameterKey": "CostReportsEnabled", "ParameterValue": "false"}, params)
        self.assertTrue(execute.call_args.kwargs["forbid_instance_change"])

    def test_detachment_allows_only_the_volume_attachment_removal(self):
        attachment = {"ResourceChange": {
            "Action": "Remove", "LogicalResourceId": "DataAttachment",
            "ResourceType": "AWS::EC2::VolumeAttachment",
        }}
        deploy.assert_safe_detachment([attachment])
        unsafe = [
            [],
            [attachment, {"ResourceChange": {"Action": "Remove", "LogicalResourceId": "DataVolume"}}],
            [{"ResourceChange": {
                "Action": "Remove", "LogicalResourceId": "DataVolume", "ResourceType": "AWS::EC2::Volume",
            }}],
            [{"ResourceChange": {
                "Action": "Modify", "LogicalResourceId": "DataAttachment",
                "ResourceType": "AWS::EC2::VolumeAttachment",
            }}],
        ]
        for changes in unsafe:
            with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                deploy.assert_safe_detachment(changes)

    def test_mock_replacement_guard_preserves_volume_and_requires_stop(self):
        def change(name, action, replacement=None, details=()):
            resource_types = {
                "DashboardInstance": "AWS::EC2::Instance",
                "StaticAddressAssociation": "AWS::EC2::EIPAssociation",
                "DataAttachment": "AWS::EC2::VolumeAttachment",
                "RecoverySafetyStop": "AWS::Scheduler::Schedule",
            }
            return {"ResourceChange": {
                "LogicalResourceId": name, "Action": action, "Replacement": replacement, "Details": list(details),
                "ResourceType": resource_types.get(name, "Other"),
            }}

        instance = change("DashboardInstance", "Modify", "True", [
            {"Target": {"Name": "ImageId"}}, {"Target": {"Name": "InstanceType"}},
            {"Target": {"Name": "UserData"}},
        ])
        address = change("StaticAddressAssociation", "Modify", "Conditional", [{
            "Target": {"Name": "InstanceId"}, "CausingEntity": "DashboardInstance",
            "ChangeSource": "ResourceReference",
        }])
        safe = [instance, address, change("DataAttachment", "Add"), change("RecoverySafetyStop", "Add")]
        deploy.assert_safe_mock_replacement(safe)
        for unsafe in (
            safe + [change("DataVolume", "Modify", "False")],
            safe + [change("SecurityGroup", "Modify", "False")],
            safe + [change("Other", "Remove")],
            safe[:-1],
            [change("DashboardInstance", "Modify", "True", [{"Target": {"Name": "ImageId"}}])] + safe[1:],
        ):
            with self.subTest(changes=unsafe), self.assertRaises(RuntimeError):
                deploy.assert_safe_mock_replacement(unsafe)
        without_ip = [instance, change("DataAttachment", "Add"), change("RecoverySafetyStop", "Add")]
        deploy.assert_safe_mock_replacement(without_ip, static_ip=False)
        with self.assertRaises(RuntimeError):
            deploy.assert_safe_mock_replacement(without_ip)
        with self.assertRaises(RuntimeError):
            deploy.assert_safe_mock_replacement(safe, static_ip=False)

    def test_static_ip_release_removes_only_the_address(self):
        def change(name, resource_type, action="Remove"):
            return {"ResourceChange": {
                "Action": action, "LogicalResourceId": name, "ResourceType": resource_type,
            }}

        valid = [
            change("StaticAddressAssociation", "AWS::EC2::EIPAssociation"),
            change("StaticAddress", "AWS::EC2::EIP"),
        ]
        deploy.assert_safe_static_ip_release(valid)
        for changes in (
            valid[:1],
            valid + [change("DashboardInstance", "AWS::EC2::Instance", "Modify")],
            [change("StaticAddress", "AWS::EC2::EIP", "Modify"), valid[0]],
            valid + [change("DataVolume", "AWS::EC2::Volume")],
        ):
            with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                deploy.assert_safe_static_ip_release(changes)

    def test_static_ip_release_requires_approval_and_stopped_instance(self):
        with self.assertRaisesRegex(RuntimeError, "irreversible"):
            deploy.release_static_ip("profile", "trading-dashboard", False)
        cloudformation = Mock()
        cloudformation.describe_stacks.return_value = {"Stacks": [{
            "StackStatus": "UPDATE_COMPLETE",
            "Tags": [{"Key": "DeploymentMode", "Value": "mock"}],
            "Parameters": [{"ParameterKey": "AutomationEnabled", "ParameterValue": "true"}],
            "Outputs": [
                {"OutputKey": "InstanceId", "OutputValue": "i-example"},
                {"OutputKey": "StaticOutboundIp", "OutputValue": "203.0.113.10"},
            ],
        }]}
        ec2 = Mock()
        ec2.describe_instances.return_value = {
            "Reservations": [{"Instances": [{"State": {"Name": "running"}}]}]
        }
        session = Mock()
        session.client.side_effect = {"cloudformation": cloudformation, "ec2": ec2}.__getitem__
        with (
            patch.object(deploy.boto3, "Session", return_value=session),
            patch.object(deploy, "execute_reviewed_update") as execute,
        ):
            with self.assertRaisesRegex(RuntimeError, "Stop the mock instance"):
                deploy.release_static_ip("profile", "trading-dashboard", True)
            execute.assert_not_called()
            ec2.describe_instances.return_value["Reservations"][0]["Instances"][0]["State"]["Name"] = "stopped"
            ec2.describe_addresses.return_value = {"Addresses": []}
            execute.side_effect = lambda *args, **kwargs: cloudformation.describe_stacks.return_value[
                "Stacks"][0].update(Outputs=[{"OutputKey": "InstanceId", "OutputValue": "i-example"}])
            deploy.release_static_ip("profile", "trading-dashboard", True)
        params = execute.call_args.args[2]
        self.assertIn({"ParameterKey": "StaticIpEnabled", "ParameterValue": "false"}, params)
        self.assertIn({"ParameterKey": "AutomationEnabled", "UsePreviousValue": True}, params)
        self.assertIs(execute.call_args.kwargs["review"], deploy.assert_safe_static_ip_release)

    def test_static_ip_change_adds_or_removes_only_the_address_delta(self):
        def change(name, action):
            kind = {**deploy.PRIMARY_STATIC_IP_RESOURCES, **deploy.SECONDARY_STATIC_IP_RESOURCES}[name]
            return {"ResourceChange": {"Action": action, "LogicalResourceId": name, "ResourceType": kind}}

        all_two = [change(name, "Add") for name in deploy.static_ip_resources(2)]
        deploy.assert_safe_static_ip_change(all_two, 0, 2)
        secondary = [change(name, "Add") for name in deploy.SECONDARY_STATIC_IP_RESOURCES]
        deploy.assert_safe_static_ip_change(secondary, 1, 2)
        deploy.assert_safe_static_ip_change(
            [change(name, "Remove") for name in deploy.static_ip_resources(2)], 2, 0
        )
        instance = {"ResourceChange": {
            "Action": "Modify", "LogicalResourceId": "DashboardInstance", "ResourceType": "AWS::EC2::Instance",
        }}
        for changes, current, target in (
            (all_two[:-1], 0, 2),
            (all_two + [instance], 0, 2),
            (all_two, 1, 2),
            ([change("StaticAddress", "Modify")] + all_two[1:], 0, 2),
            (all_two + all_two[:1], 0, 2),
        ):
            with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                deploy.assert_safe_static_ip_change(changes, current, target)
        self.assertEqual(
            deploy.static_ip_parameters(1), {"StaticIpEnabled": "true", "SecondaryStaticIpEnabled": "false"}
        )
        self.assertEqual(deploy.current_static_ip_count({"StaticIpEnabled": "false"}), 0)
        self.assertEqual(deploy.current_static_ip_count({}), 1)
        with self.assertRaises(ValueError):
            deploy.static_ip_parameters(3)

    def test_set_static_ips_preview_needs_no_approval_but_purchase_does(self):
        with self.assertRaisesRegex(RuntimeError, "billed every hour"):
            deploy.set_static_ips("profile", "trading-dashboard", 2, False, False)
        cloudformation = Mock()
        cloudformation.describe_stacks.return_value = {"Stacks": [{
            "StackStatus": "UPDATE_COMPLETE",
            "Parameters": [
                {"ParameterKey": "AutomationEnabled", "ParameterValue": "true"},
                {"ParameterKey": "StaticIpEnabled", "ParameterValue": "false"},
            ],
            "Outputs": [{"OutputKey": "InstanceId", "OutputValue": "i-example"}],
        }]}
        ec2 = Mock()
        ec2.describe_instances.return_value = {
            "Reservations": [{"Instances": [{"State": {"Name": "running"}}]}]
        }
        session = Mock()
        session.client.side_effect = {"cloudformation": cloudformation, "ec2": ec2}.__getitem__
        with (
            patch.object(deploy.boto3, "Session", return_value=session),
            patch.object(deploy, "execute_reviewed_update") as execute,
        ):
            deploy.set_static_ips("profile", "trading-dashboard", 2, False, True)
            self.assertTrue(execute.call_args.kwargs["preview_only"])
            ec2.describe_instances.assert_not_called()
            execute.reset_mock()
            with self.assertRaisesRegex(RuntimeError, "Stop the instance"):
                deploy.set_static_ips("profile", "trading-dashboard", 2, True, False)
            execute.assert_not_called()
            ec2.describe_instances.return_value["Reservations"][0]["Instances"][0]["State"]["Name"] = "stopped"
            ec2.describe_addresses.return_value = {"Addresses": [{}, {}]}
            execute.side_effect = lambda *args, **kwargs: cloudformation.describe_stacks.return_value[
                "Stacks"][0].update(Outputs=[
                    {"OutputKey": "InstanceId", "OutputValue": "i-example"},
                    {"OutputKey": "StaticOutboundIp", "OutputValue": "203.0.113.10"},
                    {"OutputKey": "SecondaryStaticOutboundIp", "OutputValue": "203.0.113.11"},
                ])
            deploy.set_static_ips("profile", "trading-dashboard", 2, True, False)
        params = execute.call_args.args[2]
        self.assertIn({"ParameterKey": "StaticIpEnabled", "ParameterValue": "true"}, params)
        self.assertIn({"ParameterKey": "SecondaryStaticIpEnabled", "ParameterValue": "true"}, params)
        self.assertIn({"ParameterKey": "AutomationEnabled", "UsePreviousValue": True}, params)
        self.assertFalse(execute.call_args.kwargs["preview_only"])
        review = execute.call_args.kwargs["review"]
        with self.assertRaises(RuntimeError):
            review([])

    def test_preview_deletes_the_change_set_without_executing(self):
        cloudformation = Mock()
        cloudformation.create_change_set.return_value = {"Id": "cs-1"}
        cloudformation.describe_change_set.return_value = {"Changes": []}
        deploy.execute_reviewed_update(cloudformation, "trading-dashboard", [], review=lambda c: None, preview_only=True)
        cloudformation.execute_change_set.assert_not_called()
        cloudformation.delete_change_set.assert_called_once_with(StackName="trading-dashboard", ChangeSetName="cs-1")

    def test_live_credentials_are_validated_and_never_mock(self):
        base = (
            "DHAN_CLIENT_ID=1100000001\nDHAN_ACCESS_TOKEN=real-token\nDHAN_CHILD_CLIENT_ID=1100000002\n"
            "DHAN_CHILD_ACCESS_TOKEN=child-token\nAPP_ORDER_PIN=482913\nTELEGRAM_BOT_TOKEN=1:x\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / ".env"
            env.write_text(base, encoding="utf-8")
            creds = deploy.live_credentials(env)
            self.assertEqual(set(creds), {
                "DHAN_CLIENT_ID", "DHAN_ACCESS_TOKEN", "DHAN_CHILD_CLIENT_ID", "DHAN_CHILD_ACCESS_TOKEN", "APP_ORDER_PIN",
            })
            for bad in (
                base.replace("real-token", "mock-token"),
                base.replace("482913", "000000"),
                base.replace("DHAN_CHILD_ACCESS_TOKEN=child-token\n", ""),
                base.replace("real-token", "a$b"),
            ):
                env.write_text(bad, encoding="utf-8")
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    deploy.live_credentials(env)

    def test_live_config_command_has_no_secrets_and_valid_python(self):
        command = deploy.live_config_command("ap-south-1", "/trading-dashboard/dhan", False)
        self.assertIn("APP_DRY_RUN=false", command)
        self.assertNotIn("real-token", command)
        script = command.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
        compile(script, "remote", "exec")
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / ".env"
            env.write_text("APP_FAST_ORDERS=true\nALGO_AUTO_ARM=true\nDHAN_ACCESS_TOKEN=old\nAPP_DRY_RUN=true\n")
            local = (script.replace('"/opt/trading/backend/.env"', repr(str(env)))
                     .replace("os.chown(tmp, st.st_uid, st.st_gid)", ""))
            raw = json.dumps({"DHAN_ACCESS_TOKEN": "new"}).encode()
            with patch("subprocess.check_output", return_value=raw):
                exec(compile(local, "remote", "exec"), {})
            self.assertEqual(env.read_text().splitlines(), [
                "APP_FAST_ORDERS=true", "ALGO_AUTO_ARM=true", "DHAN_ACCESS_TOKEN=new", "APP_DRY_RUN=false"])
        with self.assertRaises(ValueError):
            deploy.live_config_command("ap-south-1", "/x; rm -rf /", False)

    def test_live_credential_grant_allows_only_instance_role(self):
        role = {"ResourceChange": {
            "Action": "Modify", "LogicalResourceId": "InstanceRole", "ResourceType": "AWS::IAM::Role",
            "Replacement": "False",
        }}
        deploy.assert_safe_live_credential_grant([role])
        other = {"ResourceChange": {**role["ResourceChange"], "LogicalResourceId": "DashboardInstance"}}
        for changes in ([], [role, role], [other]):
            with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                deploy.assert_safe_live_credential_grant(changes)
        with self.assertRaisesRegex(RuntimeError, "approved"):
            deploy.push_live_credentials("profile", "trading-dashboard", False, False)

    def test_recovery_stop_is_today_at_1600_and_has_a_cutoff(self):
        tz = ZoneInfo("Asia/Kolkata")
        self.assertEqual(
            deploy.recovery_stop_expression(datetime(2026, 11, 2, 15, 29, tzinfo=tz)),
            "at(2026-11-02T16:00:00)",
        )
        for now in (
            datetime(2026, 11, 2, 15, 30, tzinfo=tz),
            datetime(2026, 11, 7, 9, 0, tzinfo=tz),
            datetime(2026, 11, 2, 9, 0),
        ):
            with self.subTest(now=now), self.assertRaises((ValueError, RuntimeError)):
                deploy.recovery_stop_expression(now)

    def test_linux_deployment_artifacts_use_lf(self):
        with zipfile.ZipFile(BytesIO(deploy.application_archive())) as archive:
            for name in (
                "aws/trading-dashboard.service",
                "aws/nginx.conf",
                "aws/cloudwatch-agent.json",
                "aws/write_config.py",
            ):
                with self.subTest(name=name):
                    self.assertFalse(b"\r\n" in archive.read(name), name)
        bootstrap = deploy.bootstrap_artifact()
        self.assertTrue(bootstrap.startswith(b"#!/usr/bin/env bash\n"))
        self.assertNotIn(b"\r\n", bootstrap)

    def test_change_set_refuses_deletions_and_possible_replacements(self):
        deploy.assert_safe_changes([
            {"ResourceChange": {"Action": "Add", "LogicalResourceId": "NewReport"}},
            {"ResourceChange": {"Action": "Modify", "LogicalResourceId": "Existing", "Replacement": "False"}},
        ])
        for resource in (
            {"Action": "Remove", "LogicalResourceId": "Volume"},
            {"Action": "Modify", "LogicalResourceId": "Instance", "Replacement": "True"},
            {"Action": "Modify", "LogicalResourceId": "Instance", "Replacement": "Conditional"},
        ):
            with self.subTest(resource=resource), self.assertRaises(RuntimeError):
                deploy.assert_safe_changes([{"ResourceChange": resource}])

    def test_change_set_allows_only_verified_ebs_userdata_dependents(self):
        instance = {
            "Action": "Modify",
            "LogicalResourceId": "DashboardInstance",
            "Replacement": "Conditional",
            "Details": [
                {
                    "Target": {"Name": "UserData", "RequiresRecreation": "Conditionally"},
                    "CausingEntity": "AppArtifactKey",
                }
            ],
        }
        attachment = {
            "Action": "Modify",
            "LogicalResourceId": "DataAttachment",
            "Replacement": "Conditional",
            "Details": [
                {
                    "Target": {"Name": "InstanceId"},
                    "CausingEntity": "DashboardInstance",
                    "ChangeSource": "ResourceReference",
                }
            ],
        }
        changes = [{"ResourceChange": instance}, {"ResourceChange": attachment}]
        with self.assertRaises(RuntimeError):
            deploy.assert_safe_changes(changes)
        deploy.assert_safe_changes(changes, ebs_backed_instance=True)
        with self.assertRaises(RuntimeError):
            deploy.assert_safe_changes(changes, ebs_backed_instance=True, forbid_instance_change=True)
        instance["Details"][0]["Target"]["Name"] = "ImageId"
        with self.assertRaises(RuntimeError):
            deploy.assert_safe_changes(changes, ebs_backed_instance=True)


if __name__ == "__main__":
    unittest.main()
