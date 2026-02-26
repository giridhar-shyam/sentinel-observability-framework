"""Monitoring agent tools — CloudWatch alarm management and KB queries."""
from __future__ import annotations
import json
import logging
from datetime import datetime, timezone
import boto3
from botocore.exceptions import ClientError
from strands import tool

logger = logging.getLogger(__name__)

# In-memory accumulator — avoids passing large JSON as tool arguments
_result_buffer: list[dict] = []
# Tracks the last S3 path written — read by monitoring_agent.invoke()
_last_written_s3_path: list[str] = []


def _cw(region: str):
    return boto3.client("cloudwatch", region_name=region)


def _bedrock_agent(region: str):
    return boto3.client("bedrock-agent-runtime", region_name=region)


@tool
def query_knowledge_base(resource_type: str, resource_attributes: str,
                         knowledge_base_id: str, region: str) -> str:
    """Query the Bedrock Knowledge Base for recommended CloudWatch alarms for a resource type.
    resource_attributes is a JSON string of the resource's attributes dict.
    Returns a JSON string with recommended alarms list."""
    query = (
        f"What are the recommended CloudWatch alarms for {resource_type}? "
        f"Resource configuration: {resource_attributes}. "
        "List each alarm with: alarm_name, metric_name, namespace, statistic, "
        "comparison_operator, threshold, evaluation_periods, period_seconds, description."
    )
    try:
        resp = _bedrock_agent(region).retrieve(
            knowledgeBaseId=knowledge_base_id,
            retrievalQuery={"text": query},
            retrievalConfiguration={"vectorSearchConfiguration": {"numberOfResults": 5}},
        )
        passages = [r["content"]["text"] for r in resp.get("retrievalResults", [])]
        return json.dumps({"query": query, "passages": passages})
    except Exception as e:
        logger.warning("KB query failed: %s", e)
        return json.dumps({"query": query, "passages": [], "error": str(e)})


@tool
def list_existing_alarms(resource_name: str, resource_type: str, region: str) -> str:
    """List existing CloudWatch alarms that reference a specific resource.
    Returns a JSON list of existing alarm names and their states."""
    try:
        cw = _cw(region)
        prefix = resource_name[:64]
        kwargs = {"AlarmNamePrefix": prefix} if prefix else {}
        resp = cw.describe_alarms(**kwargs)
        alarms = []
        for alarm in resp.get("MetricAlarms", []):
            dims = {d["Name"]: d["Value"] for d in alarm.get("Dimensions", [])}
            if resource_name in alarm["AlarmName"] or resource_name in dims.values():
                alarms.append({
                    "alarm_name": alarm["AlarmName"],
                    "metric_name": alarm["MetricName"],
                    "namespace": alarm["Namespace"],
                    "state": alarm["StateValue"],
                    "threshold": alarm.get("Threshold"),
                    "dimensions": dims,
                    "alarm_actions": alarm.get("AlarmActions", []),
                })
        return json.dumps(alarms)
    except Exception as e:
        return json.dumps({"error": str(e)})


@tool
def create_cloudwatch_alarm(
    alarm_name: str, metric_name: str, namespace: str, dimensions_json: str,
    statistic: str, comparison_operator: str, threshold: float,
    evaluation_periods: int, period_seconds: int, description: str,
    sns_topic_arn: str, region: str,
) -> str:
    """Create a CloudWatch alarm with an SNS action.
    dimensions_json is a JSON list of {Name, Value} dicts.
    Returns 'created' or an error message."""
    try:
        dimensions = json.loads(dimensions_json)
        _cw(region).put_metric_alarm(
            AlarmName=alarm_name, AlarmDescription=description,
            MetricName=metric_name, Namespace=namespace, Dimensions=dimensions,
            Statistic=statistic, ComparisonOperator=comparison_operator,
            Threshold=threshold, EvaluationPeriods=evaluation_periods, Period=period_seconds,
            AlarmActions=[sns_topic_arn], OKActions=[sns_topic_arn],
            TreatMissingData="notBreaching",
        )
        logger.info("Created alarm: %s", alarm_name)
        return "created"
    except ClientError as e:
        return f"error: {e.response['Error']['Message']}"


@tool
def add_sns_action_to_alarm(alarm_name: str, sns_topic_arn: str, region: str) -> str:
    """Add an SNS action to an existing CloudWatch alarm if not already present."""
    try:
        cw = _cw(region)
        resp = cw.describe_alarms(AlarmNames=[alarm_name])
        alarms = resp.get("MetricAlarms", [])
        if not alarms:
            return f"error: alarm {alarm_name} not found"
        alarm = alarms[0]
        actions = set(alarm.get("AlarmActions", []))
        if sns_topic_arn in actions:
            return "already_set"
        actions.add(sns_topic_arn)
        cw.put_metric_alarm(
            AlarmName=alarm["AlarmName"], AlarmDescription=alarm.get("AlarmDescription", ""),
            MetricName=alarm["MetricName"], Namespace=alarm["Namespace"],
            Dimensions=alarm.get("Dimensions", []), Statistic=alarm.get("Statistic", "Average"),
            ComparisonOperator=alarm["ComparisonOperator"], Threshold=alarm["Threshold"],
            EvaluationPeriods=alarm["EvaluationPeriods"], Period=alarm["Period"],
            AlarmActions=list(actions), TreatMissingData=alarm.get("TreatMissingData", "notBreaching"),
        )
        return "updated"
    except ClientError as e:
        return f"error: {e.response['Error']['Message']}"


@tool
def read_discovery_file(bucket_name: str, s3_key: str, region: str) -> str:
    """Read a discovery result JSON file from S3.
    Returns a compact JSON with only the fields needed for alarm processing:
    a list of {id, type, name, region, attributes} for each resource."""
    try:
        obj = boto3.client("s3", region_name=region).get_object(Bucket=bucket_name, Key=s3_key)
        data = json.loads(obj["Body"].read().decode("utf-8"))
        # Return only the fields the monitoring agent needs — keeps context window small
        compact = [
            {"id": r["id"], "type": r["type"], "name": r["name"],
             "region": r.get("region", region), "attributes": r.get("attributes", {})}
            for r in data.get("resources", [])
        ]
        return json.dumps({"s3_key": s3_key, "resources": compact})
    except Exception as e:
        return json.dumps({"error": str(e)})


@tool
def list_discovery_files(bucket_name: str, prefix: str, region: str) -> str:
    """List discovery result files in an S3 bucket under a prefix.
    Returns a JSON list of {key, last_modified} sorted newest first."""
    try:
        s3 = boto3.client("s3", region_name=region)
        resp = s3.list_objects_v2(Bucket=bucket_name, Prefix=prefix)
        files = [
            {"key": o["Key"], "last_modified": o["LastModified"].isoformat()}
            for o in resp.get("Contents", []) if o["Key"].endswith(".json")
        ]
        files.sort(key=lambda x: x["last_modified"], reverse=True)
        return json.dumps(files)
    except Exception as e:
        return json.dumps({"error": str(e)})


@tool
def record_resource_result(
    resource_id: str, resource_type: str, resource_name: str,
    recommended_alarms: str, existing_alarms: str,
    created_alarms: str, errors: str,
) -> str:
    """Record the alarm audit result for one resource into the in-memory buffer.
    All list arguments are JSON strings (e.g. '["alarm1","alarm2"]').
    Call this once per resource after processing its alarms.
    Returns 'recorded (N total so far)' or an error."""
    try:
        _result_buffer.append({
            "resource_id": resource_id,
            "resource_type": resource_type,
            "resource_name": resource_name,
            "recommended_alarms": json.loads(recommended_alarms),
            "existing_alarms": json.loads(existing_alarms),
            "created_alarms": json.loads(created_alarms),
            "errors": json.loads(errors),
        })
        return f"recorded ({len(_result_buffer)} total so far)"
    except Exception as e:
        return f"error: {e}"


@tool
def finalize_monitoring_result(discovery_file_key: str, bucket_name: str, s3_key: str, region: str) -> str:
    """Write the accumulated monitoring results to S3 and clear the buffer.
    s3_key is the full key path (e.g. sessions/<id>/monitoring/alarm_audit.json).
    Call this ONCE after ALL resources have been processed via record_resource_result.
    Returns 's3://<bucket>/<key>' on success or an error message."""
    total_created = sum(len(r["created_alarms"]) for r in _result_buffer)
    result = {
        "metadata": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "discovery_file": discovery_file_key,
            "total_resources": len(_result_buffer),
            "total_alarms_created": total_created,
        },
        "results": list(_result_buffer),
    }
    try:
        boto3.client("s3", region_name=region).put_object(
            Bucket=bucket_name, Key=s3_key,
            Body=json.dumps(result, indent=2),
            ContentType="application/json",
        )
        _result_buffer.clear()
        _last_written_s3_path.clear()
        _last_written_s3_path.append(f"s3://{bucket_name}/{s3_key}")
        logger.info("Monitoring results written to s3://%s/%s (%d resources, %d alarms created)",
                    bucket_name, s3_key, result["metadata"]["total_resources"], total_created)
        return f"s3://{bucket_name}/{s3_key}"
    except Exception as e:
        return f"error: S3 write failed — {e}"
