"""Remediation agent tools — CloudWatch context collection and finding report writing."""
from __future__ import annotations
import json
import logging
from datetime import datetime, timedelta, timezone

import boto3
from strands import tool

logger = logging.getLogger(__name__)


def _cw(region: str):
    return boto3.client("cloudwatch", region_name=region)


def _logs(region: str):
    return boto3.client("logs", region_name=region)


@tool
def get_alarm_details(alarm_name: str, region: str) -> str:
    """Get full details of a CloudWatch alarm including current state, threshold, and dimensions.
    Returns a JSON string with alarm configuration and current state."""
    try:
        resp = _cw(region).describe_alarms(AlarmNames=[alarm_name])
        alarms = resp.get("MetricAlarms", [])
        if not alarms:
            return json.dumps({"error": f"Alarm '{alarm_name}' not found"})
        a = alarms[0]
        return json.dumps({
            "alarm_name": a["AlarmName"],
            "description": a.get("AlarmDescription", ""),
            "state": a["StateValue"],
            "state_reason": a.get("StateReason", ""),
            "metric_name": a["MetricName"],
            "namespace": a["Namespace"],
            "dimensions": {d["Name"]: d["Value"] for d in a.get("Dimensions", [])},
            "threshold": a.get("Threshold"),
            "comparison_operator": a.get("ComparisonOperator"),
            "statistic": a.get("Statistic"),
            "period_seconds": a.get("Period"),
            "evaluation_periods": a.get("EvaluationPeriods"),
            "treat_missing_data": a.get("TreatMissingData"),
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


@tool
def get_metric_history(namespace: str, metric_name: str, dimensions_json: str,
                       region: str, hours: int = 3) -> str:
    """Get recent CloudWatch metric datapoints for the alarming metric.
    dimensions_json is a JSON object of {Name: Value} pairs.
    Returns a JSON list of {timestamp, value, unit} sorted oldest-first."""
    try:
        dims = [{"Name": k, "Value": v} for k, v in json.loads(dimensions_json).items()]
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=hours)
        resp = _cw(region).get_metric_statistics(
            Namespace=namespace,
            MetricName=metric_name,
            Dimensions=dims,
            StartTime=start,
            EndTime=end,
            Period=300,  # 5-min buckets
            Statistics=["Average", "Maximum", "Sum"],
        )
        points = sorted(resp.get("Datapoints", []), key=lambda x: x["Timestamp"])
        return json.dumps([
            {
                "timestamp": p["Timestamp"].isoformat(),
                "average": round(p.get("Average", 0), 4),
                "maximum": round(p.get("Maximum", 0), 4),
                "sum": round(p.get("Sum", 0), 4),
                "unit": p.get("Unit", ""),
            }
            for p in points
        ])
    except Exception as e:
        return json.dumps({"error": str(e)})


@tool
def get_recent_logs(log_group_name: str, region: str,
                    filter_pattern: str = "ERROR", hours: int = 1) -> str:
    """Search CloudWatch Logs for recent error/warning entries related to the alarming resource.
    Returns a JSON list of up to 20 matching log events."""
    try:
        end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        start_ms = end_ms - (hours * 3600 * 1000)
        resp = _logs(region).filter_log_events(
            logGroupName=log_group_name,
            startTime=start_ms,
            endTime=end_ms,
            filterPattern=filter_pattern,
            limit=20,
        )
        events = [
            {
                "timestamp": datetime.fromtimestamp(e["timestamp"] / 1000, tz=timezone.utc).isoformat(),
                "message": e["message"].strip(),
            }
            for e in resp.get("events", [])
        ]
        return json.dumps(events)
    except Exception as e:
        return json.dumps({"error": str(e)})


@tool
def get_recent_config_changes(resource_arn: str, region: str, hours: int = 24) -> str:
    """Look up recent CloudTrail events for a resource ARN to identify recent changes.
    Returns a JSON list of up to 10 recent API calls on this resource."""
    try:
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=hours)
        ct = boto3.client("cloudtrail", region_name=region)
        resp = ct.lookup_events(
            LookupAttributes=[{"AttributeKey": "ResourceName",
                                "AttributeValue": resource_arn.split("/")[-1]}],
            StartTime=start,
            EndTime=end,
            MaxResults=10,
        )
        events = [
            {
                "time": e["EventTime"].isoformat(),
                "event_name": e["EventName"],
                "user": e.get("Username", "unknown"),
                "source": e.get("EventSource", ""),
            }
            for e in resp.get("Events", [])
        ]
        return json.dumps(events)
    except Exception as e:
        return json.dumps({"error": str(e)})


@tool
def get_session_context(session_id: str, workarea_bucket: str, region: str) -> str:
    """Read the discovery dependency graph for a session to identify downstream impact.
    Returns a compact JSON with resource dependencies relevant to the alarming resource."""
    if not session_id or not workarea_bucket:
        return json.dumps({"error": "no session_id or workarea_bucket provided"})
    try:
        from utils.session import SessionPaths
        paths = SessionPaths(workarea_bucket, session_id)
        obj = boto3.client("s3", region_name=region).get_object(
            Bucket=workarea_bucket, Key=paths.discovery_output
        )
        data = json.loads(obj["Body"].read())
        return json.dumps({
            "environment": data.get("metadata", {}).get("environment_name", ""),
            "total_resources": data.get("metadata", {}).get("total_resources_discovered", 0),
            "dependencies": data.get("dependencies", []),
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


@tool
def write_finding_report(alarm_name: str, report_json: str,
                         workarea_bucket: str, session_id: str, region: str) -> str:
    """Write the remediation finding report to the session workarea.
    report_json must be a valid JSON string with the finding structure.
    Returns the S3 URI of the written report."""
    try:
        parsed = json.loads(report_json)
    except json.JSONDecodeError as e:
        return f"error: invalid JSON — {e}"

    required = ["alarm_name", "severity", "summary", "root_cause_hypothesis",
                "evidence", "remediation_steps", "confidence"]
    missing = [k for k in required if k not in parsed]
    if missing:
        return f"error: report missing required keys {missing}"

    try:
        from utils.session import SessionPaths
        paths = SessionPaths(workarea_bucket, session_id) if session_id else None
        key = paths.remediation_output(alarm_name) if paths else \
              f"remediation/{alarm_name.replace('/', '_')}/{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"

        boto3.client("s3", region_name=region).put_object(
            Bucket=workarea_bucket, Key=key,
            Body=json.dumps(parsed, indent=2),
            ContentType="application/json",
        )
        uri = f"s3://{workarea_bucket}/{key}"
        logger.info("Finding report written to %s", uri)
        return uri
    except Exception as e:
        return f"error: S3 write failed — {e}"
