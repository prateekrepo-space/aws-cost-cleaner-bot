"""
Unit tests for Automated AWS Cost-Cleaner Bot.
Runs in 100% offline local mode with unittest.mock (₹0 / $0 cost, zero AWS credentials needed).
Covers Dev shutdown, KeepAlive skip, Prod audit-only alerting, and Slack webhooks.
"""

import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Ensure src/ is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from lambda_function import (  # noqa: E402
    build_summary_report,
    find_dev_instances_to_stop,
    find_prod_instances_for_alert,
    find_unattached_ebs_volumes,
    lambda_handler,
    parse_boolean_env,
    send_webhook_notification,
)


class TestCostCleanerLambda(unittest.TestCase):
    """Test suite for Cost-Cleaner Lambda & ECS logic."""

    def setUp(self):
        """Set up mock EC2 client and sample AWS payloads."""
        self.mock_ec2 = MagicMock()

        # Sample EC2 DescribeInstances response for Dev
        self.sample_dev_instances_page = {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": "i-001devstop",
                            "InstanceType": "t3.micro",
                            "State": {"Name": "running"},
                            "Tags": [
                                {"Key": "Name", "Value": "api-worker-dev"},
                                {"Key": "Environment", "Value": "dev"},
                            ],
                        },
                        {
                            "InstanceId": "i-002keepalive",
                            "InstanceType": "t3.small",
                            "State": {"Name": "running"},
                            "Tags": [
                                {"Key": "Name", "Value": "db-migration-dev"},
                                {"Key": "Environment", "Value": "dev"},
                                {"Key": "KeepAlive", "Value": "true"},
                            ],
                        },
                        {
                            "InstanceId": "i-003devstop",
                            "InstanceType": "t3.micro",
                            "State": {"Name": "running"},
                            "Tags": [
                                {"Key": "Name", "Value": "cache-node-dev"},
                                {"Key": "Environment", "Value": "dev"},
                                {"Key": "KeepAlive", "Value": "false"},
                            ],
                        },
                    ]
                }
            ]
        }

        # Sample EC2 DescribeInstances response for Prod (Alert Only)
        self.sample_prod_instances_page = {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": "i-999prodalert",
                            "InstanceType": "m5.large",
                            "State": {"Name": "running"},
                            "Tags": [
                                {"Key": "Name", "Value": "payment-service-prod"},
                                {"Key": "Environment", "Value": "prod"},
                            ],
                        }
                    ]
                }
            ]
        }

        # Sample EBS DescribeVolumes response (unattached status=available)
        self.sample_volumes_page = {
            "Volumes": [
                {
                    "VolumeId": "vol-001orphan",
                    "Size": 20,
                    "VolumeType": "gp3",
                    "State": "available",
                    "Tags": [{"Key": "Name", "Value": "old-log-disk"}],
                },
                {
                    "VolumeId": "vol-002orphan",
                    "Size": 50,
                    "VolumeType": "gp2",
                    "State": "available",
                    "Tags": [],
                },
            ]
        }

    def _setup_mock_paginator(self, dev_pages=None, prod_pages=None, volumes_pages=None):
        """Helper to wire up mock paginators for EC2 describe calls based on filters."""
        dev_data = dev_pages if dev_pages is not None else [self.sample_dev_instances_page]
        prod_data = prod_pages if prod_pages is not None else [self.sample_prod_instances_page]
        vol_data = volumes_pages if volumes_pages is not None else [self.sample_volumes_page]

        def get_paginator(operation_name):
            paginator = MagicMock()
            if operation_name == "describe_instances":
                def paginate(**kwargs):
                    filters = kwargs.get("Filters", [])
                    # Check if querying Environment=prod or dev
                    for f in filters:
                        if f.get("Name") == "tag:Environment":
                            if "prod" in f.get("Values", []):
                                return prod_data
                            elif "dev" in f.get("Values", []):
                                return dev_data
                    return dev_data
                paginator.paginate.side_effect = paginate
            elif operation_name == "describe_volumes":
                paginator.paginate.return_value = vol_data
            return paginator

        self.mock_ec2.get_paginator.side_effect = get_paginator

    # =========================================================================
    # Requirement (a): DRY_RUN=true does not call stop_instances
    # =========================================================================
    @patch.dict(os.environ, {"DRY_RUN": "true", "WEBHOOK_URL": ""})
    def test_dry_run_true_does_not_call_stop_instances(self):
        """Verify that when DRY_RUN=true, instances are detected but stop_instances is NEVER called."""
        self._setup_mock_paginator()

        result = lambda_handler(event={}, context=None, ec2_client=self.mock_ec2)

        # Ensure stop_instances was NEVER called
        self.mock_ec2.stop_instances.assert_not_called()

        body = result["body"]
        self.assertTrue(body["dry_run"])
        self.assertEqual(body["instances_evaluated_for_stop"], 2)  # i-001 and i-003
        self.assertEqual(body["instances_stopped"], [])  # none stopped in dry run
        self.assertIn("i-002keepalive", body["instances_skipped_keepalive"])
        self.assertIn("i-999prodalert", body["prod_instances_alerted"])

    @patch.dict(os.environ, {}, clear=True)
    def test_dry_run_defaults_to_true_when_unset(self):
        """Verify that DRY_RUN defaults to true for safety if the env var is missing."""
        self._setup_mock_paginator()

        result = lambda_handler(event={}, context=None, ec2_client=self.mock_ec2)
        self.mock_ec2.stop_instances.assert_not_called()
        self.assertTrue(result["body"]["dry_run"])

    # =========================================================================
    # Requirement (b): DRY_RUN=false stops dev instances, skips KeepAlive=true
    # =========================================================================
    @patch.dict(os.environ, {"DRY_RUN": "false", "WEBHOOK_URL": ""})
    def test_dry_run_false_stops_dev_and_skips_keepalive(self):
        """Verify that when DRY_RUN=false, only non-KeepAlive dev instances are stopped."""
        self._setup_mock_paginator()

        result = lambda_handler(event={}, context=None, ec2_client=self.mock_ec2)

        # stop_instances must be called with i-001devstop and i-003devstop
        self.mock_ec2.stop_instances.assert_called_once_with(
            InstanceIds=["i-001devstop", "i-003devstop"]
        )

        body = result["body"]
        self.assertFalse(body["dry_run"])
        self.assertEqual(body["instances_stopped"], ["i-001devstop", "i-003devstop"])
        self.assertEqual(body["instances_skipped_keepalive"], ["i-002keepalive"])

    # =========================================================================
    # Production Safety: Prod instances are ALERTED ONLY and NEVER STOPPED
    # =========================================================================
    @patch.dict(os.environ, {"DRY_RUN": "false", "WEBHOOK_URL": ""})
    def test_prod_instances_are_alerted_and_never_stopped(self):
        """Verify that Environment=prod instances are identified for Slack alert but NEVER stopped."""
        self._setup_mock_paginator()

        result = lambda_handler(event={}, context=None, ec2_client=self.mock_ec2)

        body = result["body"]
        # Prod instance must be in alerted list
        self.assertIn("i-999prodalert", body["prod_instances_alerted"])
        # Prod instance MUST NOT be in stopped instances
        self.assertNotIn("i-999prodalert", body["instances_stopped"])
        # EC2 stop_instances call must never contain prod instance id
        call_args = self.mock_ec2.stop_instances.call_args[1]["InstanceIds"]
        self.assertNotIn("i-999prodalert", call_args)

    # =========================================================================
    # Requirement (c): Unattached EBS volumes are counted accurately
    # =========================================================================
    def test_unattached_ebs_volumes_counted_accurately(self):
        """Verify that available (unattached) EBS volumes and their sizes are aggregated accurately."""
        self._setup_mock_paginator()

        volumes, total_gib = find_unattached_ebs_volumes(self.mock_ec2)

        self.assertEqual(len(volumes), 2)
        self.assertEqual(volumes[0]["volume_id"], "vol-001orphan")
        self.assertEqual(volumes[0]["size_gib"], 20)
        self.assertEqual(volumes[0]["name"], "old-log-disk")
        self.assertEqual(volumes[1]["volume_id"], "vol-002orphan")
        self.assertEqual(volumes[1]["size_gib"], 50)
        self.assertEqual(volumes[1]["name"], "unnamed")
        self.assertEqual(total_gib, 70)  # 20 + 50

    # =========================================================================
    # Slack Webhook Notification Delivery
    # =========================================================================
    @patch("urllib.request.urlopen")
    def test_send_slack_webhook_notification_success(self, mock_urlopen):
        """Test successful notification sending with Slack text payload."""
        mock_response = MagicMock()
        mock_response.getcode.return_value = 200
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        sent = send_webhook_notification("https://hooks.slack.com/services/T00/B00/X00", "Slack FinOps Report")
        self.assertTrue(sent)
        self.assertTrue(mock_urlopen.called)

        # Inspect the payload sent
        req_arg = mock_urlopen.call_args[0][0]
        self.assertEqual(req_arg.get_method(), "POST")
        payload_data = json.loads(req_arg.data.decode("utf-8"))
        self.assertEqual(payload_data["text"], "Slack FinOps Report")
        self.assertTrue(payload_data["mrkdwn"])

    def test_send_webhook_empty_url_skips(self):
        """Test that an empty or missing webhook url returns False safely without error."""
        self.assertFalse(send_webhook_notification("", "Test"))

    # =========================================================================
    # Helper & Report Parsing
    # =========================================================================
    def test_parse_boolean_env(self):
        """Test boolean env var parsing."""
        with patch.dict(os.environ, {"FLAG": "true"}):
            self.assertTrue(parse_boolean_env("FLAG"))
        with patch.dict(os.environ, {"FLAG": "1"}):
            self.assertTrue(parse_boolean_env("FLAG"))
        with patch.dict(os.environ, {"FLAG": "false"}):
            self.assertFalse(parse_boolean_env("FLAG"))
        with patch.dict(os.environ, {"FLAG": "0"}):
            self.assertFalse(parse_boolean_env("FLAG"))
        self.assertTrue(parse_boolean_env("NON_EXISTENT", default=True))
        self.assertFalse(parse_boolean_env("NON_EXISTENT", default=False))

    def test_build_summary_report_formatting(self):
        """Test report formatting contains key sections including Slack-formatted Prod alerts."""
        report = build_summary_report(
            dry_run=True,
            stopped_instances=[{"instance_id": "i-123", "name": "web", "instance_type": "t3.micro"}],
            skipped_instances=[],
            prod_instances=[{"instance_id": "i-999prod", "name": "prod-api", "instance_type": "c5.xlarge"}],
            unattached_volumes=[{"volume_id": "vol-456", "name": "disk", "size_gib": 30, "volume_type": "gp3"}],
            total_volume_gib=30,
            aws_region="us-east-1",
        )
        self.assertIn("DRY_RUN", report)
        self.assertIn("i-123", report)
        self.assertIn("Prod EC2 (Audit Only)", report)
        self.assertIn("i-999prod", report)
        self.assertIn("vol-456", report)
        self.assertIn("30 GiB", report)


if __name__ == "__main__":
    unittest.main()
