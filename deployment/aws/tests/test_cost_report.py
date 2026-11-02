import importlib.util
import sys
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location("cost_report", HERE / "cost_report.py")
cost_report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cost_report)


class CostReportTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 2, 10, 25, tzinfo=timezone.utc)

    @staticmethod
    def day(date, amount, unit="USD"):
        return {
            "TimePeriod": {"Start": date},
            "Total": {"UnblendedCost": {"Amount": amount, "Unit": unit}},
            "Estimated": True,
        }

    def test_usage_observes_ist_midnight_and_five_minute_periods(self):
        cloudwatch = Mock()
        cloudwatch.get_metric_statistics.return_value = {
            "Datapoints": [
                {"Timestamp": datetime(2026, 11, 2, 4, 0, tzinfo=timezone.utc), "SampleCount": 1.0},
                {"Timestamp": datetime(2026, 11, 2, 4, 5, tzinfo=timezone.utc), "SampleCount": 1.0},
                {"Timestamp": datetime(2026, 11, 2, 4, 5, tzinfo=timezone.utc), "SampleCount": 1.0},
                {"Timestamp": datetime(2026, 11, 2, 4, 10, tzinfo=timezone.utc), "SampleCount": 0.0},
            ]
        }
        periods = cost_report.observed_ec2_periods(cloudwatch, "i-example", self.now)
        self.assertEqual(periods, 2)
        args = cloudwatch.get_metric_statistics.call_args.kwargs
        self.assertEqual(args["StartTime"], datetime(2026, 11, 1, 18, 30, tzinfo=timezone.utc))
        self.assertEqual(args["EndTime"], self.now)
        self.assertEqual(args["Period"], 300)
        self.assertEqual(args["Dimensions"], [{"Name": "InstanceId", "Value": "i-example"}])

    def test_daily_and_month_costs_are_utc_and_decimal_exact(self):
        ce = Mock()
        ce.get_cost_and_usage.return_value = {
            "ResultsByTime": [self.day("2026-11-01", "0.0251"), self.day("2026-11-02", "0.0009")]
        }
        today, month = cost_report.reported_costs(ce, self.now)
        self.assertEqual(today, Decimal("0.0009"))
        self.assertEqual(month, Decimal("0.0260"))
        self.assertEqual(
            ce.get_cost_and_usage.call_args.kwargs["TimePeriod"],
            {"Start": "2026-11-01", "End": "2026-11-03"},
        )
        text = cost_report.report_text(self.now, 2, today, month, test=True)
        for part in (
            "TEST",
            "Current EC2 instance activity today (IST)",
            "about 10 minutes",
            "UTC day 2026-11-02: USD 0.0009",
            "month-to-date (UTC): USD 0.0260",
            "24+ hours",
            "05:30 IST",
            "whole AWS account",
            "USD 0.01",
        ):
            with self.subTest(part=part):
                self.assertIn(part, text)

    def test_missing_data_is_explicit_not_zero(self):
        ce = Mock()
        ce.get_cost_and_usage.return_value = {"ResultsByTime": [self.day("2026-11-01", "0.0251")]}
        today, month = cost_report.reported_costs(ce, self.now)
        self.assertIsNone(today)
        self.assertIsNone(month)
        text = cost_report.report_text(self.now, 0, today, month)
        self.assertIn("Current EC2 instance activity today (IST)", text)
        self.assertIn("unknown; no CPU metric periods", text)
        self.assertEqual(text.count("not yet available"), 2)
        self.assertIn("not proof of no charges", text)

    def test_initial_data_unavailable_is_reported_but_permissions_errors_raise(self):
        ce = Mock()
        ce.get_cost_and_usage.side_effect = ClientError(
            {"Error": {"Code": "DataUnavailableException", "Message": "not ready"}}, "GetCostAndUsage"
        )
        self.assertEqual(cost_report.reported_costs(ce, self.now), (None, None))
        ce.get_cost_and_usage.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "denied"}}, "GetCostAndUsage"
        )
        with self.assertRaises(ClientError):
            cost_report.reported_costs(ce, self.now)

    def test_unexpected_currency_nonfinite_and_pagination_are_rejected(self):
        cases = [
            {"ResultsByTime": [self.day("2026-11-02", "5", "INR")]},
            {"ResultsByTime": [self.day("2026-11-02", "NaN")]},
            {"ResultsByTime": [], "NextPageToken": "more"},
            {"ResultsByTime": [self.day("2026-11-02", "1"), self.day("2026-11-02", "2")]},
        ]
        for response in cases:
            with self.subTest(response=response):
                ce = Mock()
                ce.get_cost_and_usage.return_value = response
                with self.assertRaises(RuntimeError):
                    cost_report.reported_costs(ce, self.now)

    @patch.object(cost_report, "send_message")
    @patch.object(cost_report, "reported_costs", return_value=(None, None))
    @patch.object(cost_report, "observed_ec2_periods", return_value=0)
    def test_handler_sends_only_report_without_logging_credentials(self, _usage, _costs, send):
        ssm = Mock()
        ssm.get_parameter.return_value = {
            "Parameter": {"Value": '{"token":"123:secret","chat_id":"-100123"}'}
        }
        with patch.dict(cost_report.os.environ, {"INSTANCE_ID": "i-example", "TELEGRAM_PARAMETER": "/private"}):
            with patch.object(cost_report.boto3, "client", side_effect=lambda service, **_: ssm if service == "ssm" else Mock()):
                cost_report.handler({"test": True}, None)
        self.assertEqual(send.call_count, 1)
        message = send.call_args.args[2]
        self.assertIn("TEST", message)
        self.assertIn("not yet available", message)
        self.assertNotIn("123:secret", message)
        self.assertNotIn("-100123", message)


if __name__ == "__main__":
    unittest.main()
