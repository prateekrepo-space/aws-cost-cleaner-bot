"""
Automated AWS Cost-Cleaner Bot
AWS Lambda / ECS Fargate function to stop idle development EC2 instances,
audit unattached EBS volumes, and send audit-only alerts for production EC2 instances.
Optimized for Slack incoming webhooks using Python 3.12 standard library.
"""

import json
import logging
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

# Set up structured logging
logger = logging.getLogger()
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger.setLevel(logging.INFO)

# boto3 is available natively in AWS Lambda Python runtime.
try:
    import boto3
except ImportError:  # pragma: no cover - handled during local mock testing
    boto3 = None


def get_ec2_client(client_override: Optional[Any] = None) -> Any:
    """Return an EC2 client, allowing dependency injection for unit testing."""
    if client_override is not None:
        return client_override
    if boto3 is None:
        raise RuntimeError("boto3 is not available in the current environment.")
    return boto3.client("ec2")


def parse_boolean_env(var_name: str, default: bool = True) -> bool:
    """Parse a boolean environment variable safely."""
    raw_val = os.environ.get(var_name)
    if raw_val is None:
        return default
    return raw_val.strip().lower() in ("true", "1", "yes", "y", "enabled")


def find_dev_instances_to_stop(ec2_client: Any) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    """
    Find running EC2 instances tagged Environment=dev.
    Returns:
        (instances_to_stop, skipped_keepalive_instances)
    """
    instances_to_stop: List[Dict[str, str]] = []
    skipped_instances: List[Dict[str, str]] = []

    paginator = ec2_client.get_paginator("describe_instances")
    page_iterator = paginator.paginate(
        Filters=[
            {"Name": "instance-state-name", "Values": ["running"]},
            {"Name": "tag:Environment", "Values": ["dev"]},
        ]
    )

    for page in page_iterator:
        for reservation in page.get("Reservations", []):
            for instance in reservation.get("Instances", []):
                instance_id = instance.get("InstanceId", "unknown")
                tags = instance.get("Tags", [])

                # Extract Name tag if present for clearer reporting
                name_tag = next((t.get("Value", "") for t in tags if t.get("Key") == "Name"), "")

                # Check if instance is tagged KeepAlive=true (case-insensitive)
                keep_alive = any(
                    t.get("Key") == "KeepAlive" and str(t.get("Value", "")).strip().lower() == "true"
                    for t in tags
                )

                item = {
                    "instance_id": instance_id,
                    "name": name_tag or "unnamed-dev",
                    "instance_type": instance.get("InstanceType", "unknown"),
                }

                if keep_alive:
                    logger.info(
                        "Skipping instance %s (%s): Tagged KeepAlive=true",
                        instance_id,
                        item["name"],
                    )
                    skipped_instances.append(item)
                else:
                    logger.info(
                        "Identified dev instance for shutdown: %s (%s)",
                        instance_id,
                        item["name"],
                    )
                    instances_to_stop.append(item)

    return instances_to_stop, skipped_instances


def find_prod_instances_for_alert(ec2_client: Any) -> List[Dict[str, str]]:
    """
    Find running EC2 instances tagged Environment=prod for review and Slack alerting.
    SAFETY RULE: These instances are AUDIT-ONLY and are NEVER stopped.
    """
    prod_instances: List[Dict[str, str]] = []

    paginator = ec2_client.get_paginator("describe_instances")
    page_iterator = paginator.paginate(
        Filters=[
            {"Name": "instance-state-name", "Values": ["running"]},
            {"Name": "tag:Environment", "Values": ["prod"]},
        ]
    )

    for page in page_iterator:
        for reservation in page.get("Reservations", []):
            for instance in reservation.get("Instances", []):
                instance_id = instance.get("InstanceId", "unknown")
                tags = instance.get("Tags", [])
                name_tag = next((t.get("Value", "") for t in tags if t.get("Key") == "Name"), "")

                item = {
                    "instance_id": instance_id,
                    "name": name_tag or "unnamed-prod",
                    "instance_type": instance.get("InstanceType", "unknown"),
                }
                logger.info(
                    "Audited running production instance for Slack alert (AUDIT ONLY): %s (%s)",
                    instance_id,
                    item["name"],
                )
                prod_instances.append(item)

    return prod_instances


def find_unattached_ebs_volumes(ec2_client: Any) -> Tuple[List[Dict[str, Any]], int]:
    """
    Find unattached EBS volumes (status='available') without deleting them.
    Returns:
        (unattached_volumes_list, total_gib)
    """
    unattached_volumes: List[Dict[str, Any]] = []
    total_gib = 0

    paginator = ec2_client.get_paginator("describe_volumes")
    page_iterator = paginator.paginate(
        Filters=[{"Name": "status", "Values": ["available"]}]
    )

    for page in page_iterator:
        for volume in page.get("Volumes", []):
            volume_id = volume.get("VolumeId", "unknown")
            size_gib = volume.get("Size", 0)
            vol_type = volume.get("VolumeType", "unknown")
            tags = volume.get("Tags", [])
            name_tag = next((t.get("Value", "") for t in tags if t.get("Key") == "Name"), "unnamed")

            total_gib += size_gib
            logger.info(
                "Identified unattached EBS volume: %s (%s, %d GiB, type: %s)",
                volume_id,
                name_tag,
                size_gib,
                vol_type,
            )
            unattached_volumes.append(
                {
                    "volume_id": volume_id,
                    "name": name_tag,
                    "size_gib": size_gib,
                    "volume_type": vol_type,
                }
            )

    return unattached_volumes, total_gib


def build_summary_report(
    dry_run: bool,
    stopped_instances: List[Dict[str, str]],
    skipped_instances: List[Dict[str, str]],
    prod_instances: List[Dict[str, str]],
    unattached_volumes: List[Dict[str, Any]],
    total_volume_gib: int,
    aws_region: str,
) -> str:
    """Format a simple, compact, and structured Slack message."""
    mode_badge = "DRY_RUN" if dry_run else "ACTIVE"
    action_label = "Identified" if dry_run else "Stopped"
    est_monthly_waste = total_volume_gib * 0.08

    lines = [
        f":cloud: *AWS Cost-Cleaner* | `{aws_region}` | `{mode_badge}`",
        "",
        f":desktop_computer: *Dev EC2 ({action_label}):* {len(stopped_instances)}",
    ]
    if stopped_instances:
        for inst in stopped_instances:
            lines.append(f"• `{inst['instance_id']}` ({inst['name']} • {inst['instance_type']})")
    else:
        lines.append("• _None_")

    lines.append("")
    lines.append(f":shield: *Dev EC2 (KeepAlive Exempt):* {len(skipped_instances)}")
    if skipped_instances:
        for inst in skipped_instances:
            lines.append(f"• `{inst['instance_id']}` ({inst['name']} • {inst['instance_type']})")
    else:
        lines.append("• _None_")

    lines.append("")
    lines.append(f":warning: *Prod EC2 (Audit Only):* {len(prod_instances)}")
    if prod_instances:
        for pinst in prod_instances:
            lines.append(f"• `{pinst['instance_id']}` ({pinst['name']} • {pinst['instance_type']})")
    else:
        lines.append("• _None_")

    lines.append("")
    lines.append(f":floppy_disk: *Unattached EBS:* {len(unattached_volumes)} ({total_volume_gib} GiB • est. ~${est_monthly_waste:.2f}/mo)")
    if unattached_volumes:
        for vol in unattached_volumes:
            lines.append(f"• `{vol['volume_id']}` ({vol['size_gib']} GiB • {vol['volume_type']})")
    else:
        lines.append("• _None_")

    return "\n".join(lines)


def send_webhook_notification(webhook_url: str, message: str) -> bool:
    """
    Send formatted report to Slack incoming webhook using Python standard library urllib.
    Slack accepts {'text': message} or Block Kit payloads.
    """
    if not webhook_url:
        logger.info("WEBHOOK_URL is not configured; skipping Slack notification.")
        return False

    payload = {
        "text": message,
        "mrkdwn": True,
    }
    data = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(
        webhook_url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "AWS-Cost-Cleaner-Bot-Slack/2.0",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            status_code = response.getcode()
            logger.info("Slack webhook delivered successfully with status code: %s", status_code)
            return True
    except urllib.error.HTTPError as exc:
        logger.error("HTTP error sending Slack webhook: %s %s", exc.code, exc.reason)
        return False
    except urllib.error.URLError as exc:
        logger.error("Network error sending Slack webhook: %s", exc.reason)
        return False
    except Exception as exc:
        logger.error("Unexpected error sending Slack webhook: %s", str(exc))
        return False


def lambda_handler(event: Optional[Dict[str, Any]] = None, context: Optional[Any] = None, ec2_client: Optional[Any] = None) -> Dict[str, Any]:
    """
    AWS Lambda / ECS Fargate Entry Point.
    Scheduled by Amazon EventBridge.
    """
    logger.info("AWS Cost-Cleaner Bot triggered. Event: %s", json.dumps(event or {}))

    # Read configuration from environment variables
    dry_run = parse_boolean_env("DRY_RUN", default=True)
    webhook_url = os.environ.get("WEBHOOK_URL", "").strip()
    aws_region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))

    logger.info("Configuration: DRY_RUN=%s, Region=%s, SlackWebhookConfigured=%s", dry_run, aws_region, bool(webhook_url))

    client = get_ec2_client(ec2_client)

    # 1. Discover dev instances and check KeepAlive exemptions
    instances_to_stop, skipped_instances = find_dev_instances_to_stop(client)

    # 2. Stop dev instances if not in DRY_RUN mode
    stopped_instance_ids: List[str] = []
    if instances_to_stop:
        target_ids = [inst["instance_id"] for inst in instances_to_stop]
        if dry_run:
            logger.info("[DRY RUN] Would stop %d dev instance(s): %s", len(target_ids), target_ids)
        else:
            logger.info("[ACTION] Stopping %d dev instance(s): %s", len(target_ids), target_ids)
            client.stop_instances(InstanceIds=target_ids)
            stopped_instance_ids = target_ids
    else:
        logger.info("No running instances tagged Environment=dev found.")

    # 3. Discover running production instances for AUDIT & ALERT ONLY (Never stopped)
    prod_instances = find_prod_instances_for_alert(client)

    # 4. Discover unattached EBS volumes (Audit only, zero deletes)
    unattached_volumes, total_volume_gib = find_unattached_ebs_volumes(client)

    # 5. Generate Slack notification report
    report = build_summary_report(
        dry_run=dry_run,
        stopped_instances=instances_to_stop,
        skipped_instances=skipped_instances,
        prod_instances=prod_instances,
        unattached_volumes=unattached_volumes,
        total_volume_gib=total_volume_gib,
        aws_region=aws_region,
    )

    # 6. Send report via Slack Webhook
    webhook_sent = send_webhook_notification(webhook_url, report)

    summary = {
        "statusCode": 200,
        "body": {
            "dry_run": dry_run,
            "instances_evaluated_for_stop": len(instances_to_stop),
            "instances_stopped": stopped_instance_ids if not dry_run else [],
            "instances_skipped_keepalive": [inst["instance_id"] for inst in skipped_instances],
            "prod_instances_alerted": [inst["instance_id"] for inst in prod_instances],
            "unattached_ebs_volumes_count": len(unattached_volumes),
            "unattached_ebs_total_gib": total_volume_gib,
            "webhook_sent": webhook_sent,
        },
    }

    logger.info("Cost-Cleaner run finished successfully: %s", summary["body"])
    return summary


if __name__ == "__main__":
    result = lambda_handler()
    sys.exit(0 if result.get("statusCode") == 200 else 1)
