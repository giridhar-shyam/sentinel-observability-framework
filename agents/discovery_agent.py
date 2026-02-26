"""Cloud Discovery Agent — AgentCore entrypoint."""
from __future__ import annotations
import logging

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from utils.config import config
from strands import Agent
from strands.models import BedrockModel

from utils.stop_parser import STOPParser
from tools.discovery_tools import (
    assume_discovery_role,
    enumerate_ec2,
    enumerate_ecs,
    enumerate_rds,
    enumerate_s3,
    enumerate_lambda,
    enumerate_standard_services,
    enumerate_deep_services,
    write_discovery_result,
)

logging.basicConfig(level=config.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

app = BedrockAgentCoreApp()

SYSTEM_PROMPT = """You are a Cloud Discovery Agent. Your job is to discover AWS resources
defined in a STOP document and produce a structured Resource Graph JSON.

## Process
1. Call assume_discovery_role with the provided role ARN to get temporary credentials.
2. Based on discovery_depth, call the appropriate enumeration tools:
   - shallow: enumerate_ec2, enumerate_ecs, enumerate_rds, enumerate_s3, enumerate_lambda
   - standard: above + enumerate_standard_services
   - deep: above + enumerate_deep_services
3. For each entry point region, call the tools with that region and account_id.
4. Filter out any resources whose name matches entries in exclude_namespaces.
5. Reason about dependency relationships between the discovered resources.
   Use relationship types: CONTAINS, DEPENDS_ON, SERVES_TRAFFIC_FROM, STORES_DATA_FOR.
6. If hints.tech_stack is provided, use it to annotate relevant resources
   (e.g. "kafka" → MSK clusters, "postgres" → RDS instances, "redis" → ElastiCache).

## CRITICAL: Writing Output
After collecting ALL resources, call write_discovery_result ONCE with the complete JSON:
{
  "metadata": {
    "discovery_timestamp": "<ISO8601>",
    "stop_version": "1.0",
    "environment_name": "<name>",
    "discovery_depth": "<depth>",
    "total_resources_discovered": <int>,
    "scan_limit_reached": false
  },
  "resources": [{"id": "<arn>", "type": "<type>", "name": "<name>", "region": "<region>", "attributes": {}, "tags": {}}],
  "dependencies": [{"source_id": "<arn>", "target_id": "<arn>", "relationship_type": "<type>"}],
  "discovery_errors": []
}

The write_discovery_result tool validates the JSON before writing.
If it returns a validation error, fix the JSON and call it again.
Do NOT return the JSON as text — always use write_discovery_result to persist it.
Return the S3 path returned by write_discovery_result as your final response."""


@app.entrypoint
def invoke(payload: dict) -> dict:
    logger.info("Received payload keys: %s", list(payload.keys()))

    # Accept either a file path or inline JSON string
    stop_doc_path = payload.get("stop_document_path")
    stop_doc_str = payload.get("stop_document")
    role_arn = payload.get("role_arn") or config.get("DISCOVERY_ROLE_ARN", "")
    if not role_arn:
        return {"error": "DISCOVERY_ROLE_ARN is required — set it in config.properties"}

    parser = STOPParser()
    if stop_doc_path:
        doc, err = parser.parse(stop_doc_path)
    elif stop_doc_str:
        doc, err = parser.parse_from_string(stop_doc_str)
    else:
        return {"error": "stop_document_path or stop_document is required"}

    if err:
        return {"error": f"STOP parse error [{err.field}]: {err.reason}"}

    region = doc.environment.regions[0] if doc.environment.regions else "us-east-1"
    account_id = next((ep.id for ep in doc.entry_points if ep.type == "account"), "")

    # Session-scoped output path
    from utils.session import SessionPaths, new_session_id
    session_id = payload.get("session_id") or new_session_id()
    workarea = config.get("WORKAREA_BUCKET", "")
    if not workarea:
        return {"error": "WORKAREA_BUCKET is required — set it in config.properties"}
    paths = SessionPaths(workarea, session_id)
    bucket_name = workarea
    s3_key = paths.discovery_output

    prompt = (
        f"Discover AWS resources for environment '{doc.environment.name}' "
        f"(type={doc.environment.type}, depth={doc.agent_config.discovery_depth}).\n"
        f"Role ARN: {role_arn}\n"
        f"Region: {region}\n"
        f"Account ID: {account_id}\n"
        f"Exclude namespaces: {doc.hints.exclude_namespaces}\n"
        f"Tech stack hints: {doc.hints.tech_stack}\n"
        f"Known services: {doc.hints.known_services}\n"
        f"Max resources: {doc.agent_config.max_resources_scanned or 'unlimited'}\n"
        f"Results bucket: {bucket_name}\n"
        f"S3 key: {s3_key}\n"
        f"AWS region: {region}\n"
        "Discover all resources, then call write_discovery_result with the complete JSON."
    )

    agent = Agent(
        model=BedrockModel(
            model_id=config.get("MODEL_ID", "us.amazon.nova-pro-v1:0"),
            region_name=region,
        ),
        system_prompt=SYSTEM_PROMPT,
        tools=[
            assume_discovery_role,
            enumerate_ec2,
            enumerate_ecs,
            enumerate_rds,
            enumerate_s3,
            enumerate_lambda,
            enumerate_standard_services,
            enumerate_deep_services,
            write_discovery_result,
        ],
    )

    try:
        from tools.discovery_tools import _last_written_s3_path
        _last_written_s3_path.clear()
        agent(prompt)
        # Use the path recorded by write_discovery_result tool (avoids parsing verbose LLM text)
        s3_path = _last_written_s3_path[0] if _last_written_s3_path else ""
        logger.info("Discovery agent completed: %s", s3_path)
        return {"resource_graph_s3": s3_path}
    except Exception as e:
        logger.error("Agent error: %s", e, exc_info=True)
        return {"error": str(e)}


if __name__ == "__main__":
    app.run()
