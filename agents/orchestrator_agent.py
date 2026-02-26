"""Orchestrator Agent — LLM-driven pipeline coordinator with validation and retry."""
from __future__ import annotations
import json
import logging
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import boto3
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent, tool
from strands.models import BedrockModel

from utils.config import config
from utils.stop_parser import STOPParser
from utils.session import SessionPaths, new_session_id

logging.basicConfig(level=config.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

app = BedrockAgentCoreApp()

SYSTEM_PROMPT = """You are an Orchestrator Agent coordinating a multi-agent cloud observability pipeline.

## Your job
Given a STOP document and a session ID, run the pipeline in order:
1. run_discovery — discovers AWS resources and writes to the session workarea
2. validate_agent_output with required_keys="metadata,resources" — validates discovery output
3. run_monitoring — processes the discovery output and provisions CloudWatch alarms
4. validate_agent_output with required_keys="metadata,results" — validates monitoring output
5. write_run_summary — writes the final pipeline summary

## Output schemas (use EXACTLY these keys for validation)
- Discovery output required keys: "metadata,resources"
- Monitoring output required keys: "metadata,results"

## Decision rules
- If validate_agent_output returns an error, call the failed agent again (max 1 retry per step).
- If discovery returns 0 resources, log a warning but continue to monitoring.
- If any step fails after 1 retry, call write_run_summary with status="failed" and stop.
- Always call write_run_summary as the final step."""


@tool
def run_discovery(session_id: str, stop_document: str, role_arn: str) -> str:
    """Run the discovery agent for the given session and STOP document.
    Returns a JSON string with 'resource_graph_s3' or 'error'."""
    from agents.discovery_agent import invoke
    result = invoke({
        "stop_document": stop_document,
        "role_arn": role_arn,
        "session_id": session_id,
    })
    logger.info("[%s] Discovery result: %s", session_id, result)
    return json.dumps(result)


@tool
def run_monitoring(session_id: str) -> str:
    """Run the monitoring agent for the given session.
    Returns a JSON string with 'monitoring_result_s3' or 'error'."""
    from agents.monitoring_agent import invoke
    result = invoke({"session_id": session_id})
    logger.info("[%s] Monitoring result: %s", session_id, result)
    return json.dumps(result)


@tool
def run_remediation(alarm_event_json: str) -> str:
    """Run the remediation agent for a CloudWatch alarm breach event.
    alarm_event_json is a JSON string with alarm_name, metric details, and optional session_id.
    Returns a JSON string with 'finding_report_s3' or 'error'."""
    from agents.remediation_agent import invoke
    alarm_event = json.loads(alarm_event_json)
    result = invoke({"alarm_event": alarm_event})
    logger.info("Remediation result: %s", result)
    return json.dumps(result)
    """Validate that an S3 file is valid JSON containing all required_keys.
    required_keys is a comma-separated string.
    For discovery output use: 'metadata,resources'
    For monitoring output use: 'metadata,results'
    Returns 'valid: N resources/results' or an error description."""
    if not s3_uri or not s3_uri.startswith("s3://"):
        return f"error: invalid S3 URI '{s3_uri}'"
    try:
        parts = s3_uri.replace("s3://", "").split("/", 1)
        bucket, key = parts[0], parts[1]
        obj = boto3.client("s3", region_name=config.get("AWS_REGION", "us-east-1")).get_object(
            Bucket=bucket, Key=key
        )
        data = json.loads(obj["Body"].read())
        keys = [k.strip() for k in required_keys.split(",")]
        missing = [k for k in keys if k not in data]
        if missing:
            actual_keys = list(data.keys())
            return f"error: missing keys {missing}. Actual keys present: {actual_keys}"
        if "resources" in data:
            n = len(data["resources"])
            if not isinstance(data["resources"], list):
                return "error: 'resources' is not a list"
            return f"valid: {n} resources"
        if "results" in data:
            n = len(data["results"])
            if not isinstance(data["results"], list):
                return "error: 'results' is not a list"
            return f"valid: {n} results"
        return "valid"
    except Exception as e:
        return f"error: {e}"


@tool
def write_run_summary(session_id: str, status: str, steps: str, notes: str) -> str:
    """Write the orchestrator run summary to the session workarea.
    steps is a JSON string of step results. status is 'success' or 'failed'.
    Returns the S3 URI of the summary."""
    workarea = config.get("WORKAREA_BUCKET", "")
    region = config.get("AWS_REGION", "us-east-1")
    if not workarea:
        return "error: WORKAREA_BUCKET not configured"
    paths = SessionPaths(workarea, session_id)
    summary = {
        "session_id": session_id,
        "status": status,
        "steps": json.loads(steps) if steps else [],
        "notes": notes,
    }
    try:
        boto3.client("s3", region_name=region).put_object(
            Bucket=workarea, Key=paths.orchestrator_summary,
            Body=json.dumps(summary, indent=2),
            ContentType="application/json",
        )
        uri = paths.s3_uri(paths.orchestrator_summary)
        logger.info("[%s] Run summary written to %s", session_id, uri)
        return uri
    except Exception as e:
        return f"error: {e}"


@app.entrypoint
def invoke(payload: dict) -> dict:
    logger.info("Orchestrator received payload keys: %s", list(payload.keys()))

    stop_doc_str = payload.get("stop_document")
    stop_doc_path = payload.get("stop_document_path")
    session_id = payload.get("session_id") or new_session_id()

    parser = STOPParser()
    if stop_doc_path:
        doc, err = parser.parse(stop_doc_path)
        stop_doc_str = stop_doc_str or open(stop_doc_path).read()
    elif stop_doc_str:
        doc, err = parser.parse_from_string(stop_doc_str)
    else:
        return {"error": "stop_document or stop_document_path is required"}

    if err:
        return {"error": f"STOP parse error [{err.field}]: {err.reason}"}

    # Save STOP doc to session workarea for traceability
    workarea = config.get("WORKAREA_BUCKET", "")
    region = config.get("AWS_REGION", "us-east-1")
    if workarea:
        paths = SessionPaths(workarea, session_id)
        try:
            boto3.client("s3", region_name=region).put_object(
                Bucket=workarea, Key=paths.stop_doc,
                Body=stop_doc_str, ContentType="application/json",
            )
        except Exception as e:
            logger.warning("Could not save STOP doc to workarea: %s", e)

    role_arn = config.get("DISCOVERY_ROLE_ARN", "")
    logger.info("[%s] Starting pipeline for environment '%s'", session_id, doc.environment.name)

    agent = Agent(
        model=BedrockModel(
            model_id=config.get("MODEL_ID", "us.amazon.nova-pro-v1:0"),
            region_name=region,
        ),
        system_prompt=SYSTEM_PROMPT,
        tools=[run_discovery, run_monitoring, run_remediation, validate_agent_output, write_run_summary],
    )

    workarea_uri = f"s3://{workarea}/sessions/{session_id}" if workarea else "n/a"
    prompt = (
        f"Run the observability pipeline for session '{session_id}'.\n"
        f"Environment: {doc.environment.name} ({doc.environment.type})\n"
        f"Role ARN: {role_arn}\n"
        f"STOP document: {stop_doc_str}\n"
        f"Session workarea: {workarea_uri}\n"
        f"Discovery output will be at: {workarea_uri}/discovery/resource_graph.json\n"
        f"Monitoring output will be at: {workarea_uri}/monitoring/alarm_audit.json\n"
        "Run the pipeline now."
    )

    try:
        response = agent(prompt)
        return {
            "session_id": session_id,
            "environment": doc.environment.name,
            "workarea": workarea_uri,
            "summary": str(response),
        }
    except Exception as e:
        logger.error("[%s] Orchestrator error: %s", session_id, e, exc_info=True)
        return {"error": str(e), "session_id": session_id}


if __name__ == "__main__":
    app.run()
