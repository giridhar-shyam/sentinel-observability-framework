"""Remediation Agent — analyzes CloudWatch alarm breaches and produces finding reports."""
from __future__ import annotations
import json
import logging

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent
from strands.models import BedrockModel

from utils.config import config
from tools.remediation_tools import (
    get_alarm_details,
    get_metric_history,
    get_recent_logs,
    get_recent_config_changes,
    get_session_context,
    write_finding_report,
)

logging.basicConfig(level=config.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

app = BedrockAgentCoreApp()

SYSTEM_PROMPT = """You are a Remediation Agent assisting SRE engineers in diagnosing CloudWatch alarm breaches.

## Your job
Given an alarm event, produce a structured finding report that helps the SRE quickly understand
what happened and what to do about it.

## Process
1. Call get_alarm_details to understand the alarm configuration and current state.
2. Call get_metric_history to see the metric trend leading up to the breach.
3. Infer the log group from the alarm dimensions:
   - Lambda: /aws/lambda/<FunctionName>
   - ECS: /ecs/<ClusterName>
   - RDS: /aws/rds/instance/<DBInstanceIdentifier>/error
   - API Gateway: API-Gateway-Execution-Logs_<api-id>/<stage>
   Then call get_recent_logs with the inferred log group.
4. Call get_recent_config_changes using the primary resource identifier from dimensions.
5. If a session_id is provided, call get_session_context to check for downstream dependencies.
6. Synthesize all evidence into a finding report and call write_finding_report.

## Finding report schema
{
  "alarm_name": "<name>",
  "severity": "critical|high|medium|low",
  "summary": "<one sentence: what happened>",
  "root_cause_hypothesis": "<most likely cause based on evidence>",
  "evidence": {
    "metric_trend": "<describe the trend: spike, gradual increase, sustained high, etc.>",
    "recent_changes": "<any config changes in last 24h, or 'none detected'>",
    "log_signals": "<relevant log patterns, or 'not checked'>",
    "downstream_impact": "<resources that depend on this one, or 'none'>"
  },
  "remediation_steps": [
    "<step 1: most likely fix>",
    "<step 2: if step 1 doesn't resolve>",
    "<step 3: escalation path>"
  ],
  "confidence": "high|medium|low",
  "confidence_reason": "<why you have this confidence level>"
}

## Rules
- Base severity on: metric breach magnitude + environment type (production = higher severity)
- Be specific — reference actual metric values, timestamps, and resource names
- If a log group returns an error, note it in log_signals and continue — do not stop
- If evidence is insufficient, say so in confidence_reason rather than guessing
- Always call write_finding_report as the final step"""


@app.entrypoint
def invoke(payload: dict) -> dict:
    logger.info("Remediation agent received: %s", list(payload.keys()))

    region = config.get("AWS_REGION", "us-east-1")
    workarea = config.get("WORKAREA_BUCKET", "")

    # Accept alarm event either directly or wrapped in SNS envelope
    alarm_event = payload.get("alarm_event") or payload
    if isinstance(alarm_event, str):
        alarm_event = json.loads(alarm_event)

    alarm_name = alarm_event.get("alarm_name") or alarm_event.get("AlarmName", "")
    if not alarm_name:
        return {"error": "alarm_name is required in alarm_event"}

    session_id = alarm_event.get("session_id", "")

    prompt = (
        f"Analyze this CloudWatch alarm breach and produce a finding report.\n\n"
        f"Alarm name: {alarm_name}\n"
        f"Region: {region}\n"
        f"Workarea bucket: {workarea}\n"
        f"Session ID (optional context): {session_id or 'not provided'}\n\n"
        f"Full alarm event:\n{json.dumps(alarm_event, indent=2)}\n\n"
        "Collect evidence and write the finding report."
    )

    agent = Agent(
        model=BedrockModel(
            model_id=config.get("MONITORING_MODEL_ID", "us.anthropic.claude-3-5-sonnet-20241022-v2:0"),
            region_name=region,
        ),
        system_prompt=SYSTEM_PROMPT,
        tools=[
            get_alarm_details,
            get_metric_history,
            get_recent_logs,
            get_recent_config_changes,
            get_session_context,
            write_finding_report,
        ],
    )

    try:
        agent(prompt)
        # Extract the S3 URI from the last write_finding_report call
        # (agent response text may be verbose — we check the tool output directly)
        report_uri = _last_report_uri(alarm_name, workarea, session_id, region)
        logger.info("Remediation agent completed: %s", report_uri)
        return {"finding_report_s3": report_uri, "alarm_name": alarm_name, "session_id": session_id}
    except Exception as e:
        logger.error("Remediation agent error: %s", e, exc_info=True)
        return {"error": str(e)}


def _last_report_uri(alarm_name: str, workarea: str, session_id: str, region: str) -> str:
    """Check S3 for the written report — more reliable than parsing LLM response text."""
    if not workarea:
        return ""
    try:
        from utils.session import SessionPaths
        if session_id:
            paths = SessionPaths(workarea, session_id)
            key = paths.remediation_output(alarm_name)
        else:
            safe = alarm_name.replace("/", "_").replace(" ", "_")
            key = f"remediation/{safe}/"
            # List to find the latest
            import boto3
            resp = boto3.client("s3", region_name=region).list_objects_v2(
                Bucket=workarea, Prefix=key
            )
            objects = sorted(resp.get("Contents", []), key=lambda x: x["LastModified"], reverse=True)
            if not objects:
                return ""
            key = objects[0]["Key"]
        return f"s3://{workarea}/{key}"
    except Exception:
        return ""


if __name__ == "__main__":
    app.run()
