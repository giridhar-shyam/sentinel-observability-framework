"""Monitoring Agent — ensures recommended CloudWatch alarms exist for discovered resources."""
from __future__ import annotations
import logging

import boto3
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from utils.config import config
from strands import Agent
from strands.models import BedrockModel

from tools.monitoring_tools import (
    query_knowledge_base,
    list_existing_alarms,
    create_cloudwatch_alarm,
    add_sns_action_to_alarm,
    read_discovery_file,
    list_discovery_files,
    record_resource_result,
)

logging.basicConfig(level=config.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

app = BedrockAgentCoreApp()

SYSTEM_PROMPT = """You are a Monitoring Agent. Your job is to ensure all discovered AWS resources
have the recommended CloudWatch alarms provisioned.

## Process — repeat for EVERY resource in the discovery file:
1. Call query_knowledge_base with the resource type and attributes to get recommended alarms.
2. Call list_existing_alarms to check which alarms already exist for this resource.
3. For each recommended alarm that is MISSING: call create_cloudwatch_alarm.
4. For each recommended alarm that EXISTS but lacks the SNS action: call add_sns_action_to_alarm.
5. Call record_resource_result with the summary for this resource (all list args as JSON strings).

## Rules
- Process EVERY resource in the list — do not skip any.
- Call record_resource_result after each resource before moving to the next.
- When all resources are done, respond with: "DONE: processed N resources"
- Do NOT call finalize_monitoring_result — it is called automatically after you finish."""


@app.entrypoint
def invoke(payload: dict) -> dict:
    logger.info("Monitoring agent received: %s", list(payload.keys()))

    region = config.get("AWS_REGION", "us-east-1")
    kb_id = config.get("KNOWLEDGE_BASE_ID", "")
    sns_arn = config.get("ALARMS_NOTIFICATION_TOPIC_ARN", "")

    for key in ["KNOWLEDGE_BASE_ID", "ALARMS_NOTIFICATION_TOPIC_ARN"]:
        if not config.get(key):
            return {"error": f"{key} is required — set it in config.properties"}

    # Session-scoped paths
    from utils.session import SessionPaths, new_session_id
    session_id = payload.get("session_id") or new_session_id()
    workarea = config.get("WORKAREA_BUCKET", "")
    if not workarea:
        return {"error": "WORKAREA_BUCKET is required — set it in config.properties"}
    paths = SessionPaths(workarea, session_id)
    discovery_bucket = workarea
    discovery_key = payload.get("s3_key") or paths.discovery_output
    results_bucket = workarea
    results_key = paths.monitoring_output

    s3_key = discovery_key
    env_prefix = payload.get("env_prefix", "")

    prompt = (
        f"Process the discovery file from S3.\n"
        f"Discovery bucket: {discovery_bucket}\n"
        f"Discovery S3 key: {s3_key or 'unknown — use list_discovery_files to find the latest'}\n"
        f"Env prefix for listing: {env_prefix or '(list all)'}\n"
        f"Knowledge base ID: {kb_id}\n"
        f"SNS topic ARN: {sns_arn}\n"
        f"Region: {region}\n"
        f"Results bucket (for finalize_monitoring_result): {results_bucket}\n\n"
        "Steps:\n"
        "1. If s3_key is unknown, call list_discovery_files to find the latest file.\n"
        "2. Call read_discovery_file with the discovery bucket and key.\n"
        "3. For EACH resource in resources[], process alarms and call record_resource_result.\n"
        "4. After ALL resources are processed, call finalize_monitoring_result with:\n"
        f"   - discovery_file_key: the s3 key used\n"
        f"   - bucket_name: {results_bucket}\n"
        f"   - region: {region}\n"
        "5. Return the S3 path from finalize_monitoring_result."
    )

    agent = Agent(
        model=BedrockModel(
            model_id=config.get("MONITORING_MODEL_ID", "us.anthropic.claude-3-5-sonnet-20241022-v2:0"),
            region_name=region,
        ),
        system_prompt=SYSTEM_PROMPT,
        tools=[
            query_knowledge_base,
            list_existing_alarms,
            create_cloudwatch_alarm,
            add_sns_action_to_alarm,
            read_discovery_file,
            list_discovery_files,
            record_resource_result,
            # finalize_monitoring_result is called by invoke() after agent completes
        ],
    )

    try:
        from tools.monitoring_tools import _result_buffer, _last_written_s3_path
        _result_buffer.clear()
        _last_written_s3_path.clear()
        agent(prompt)
        from tools.monitoring_tools import finalize_monitoring_result
        finalize_monitoring_result(
            discovery_file_key=s3_key or "unknown",
            bucket_name=results_bucket,
            s3_key=results_key,
            region=region,
        )
        s3_path = _last_written_s3_path[0] if _last_written_s3_path else ""
        logger.info("Monitoring agent completed: %s", s3_path)
        return {"monitoring_result_s3": s3_path}
    except Exception as e:
        logger.error("Monitoring agent error: %s", e, exc_info=True)
        return {"error": str(e)}


if __name__ == "__main__":
    app.run()
