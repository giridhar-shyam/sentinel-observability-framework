#!/usr/bin/env python3
"""Invoke any agent in the sentinel-strands pipeline — local or AgentCore runtime."""
import json
import sys
import os
import boto3

sys.path.insert(0, os.path.dirname(__file__))

from utils.config import config


def _require(key: str) -> str:
    val = config.get(key, "")
    if not val:
        print(f"ERROR: '{key}' is required — set it in config.properties or as an env var.")
        sys.exit(1)
    return val


def _invoke_agentcore(arn_key: str, payload: str) -> None:
    region = _require("AWS_REGION")
    client = boto3.client("bedrock-agentcore", region_name=region)
    resp = client.invoke_agent_runtime(
        agentRuntimeArn=_require(arn_key),
        qualifier="DEFAULT",
        payload=payload,
        runtimeSessionId="session-001",
    )
    result = b"".join(resp["response"]).decode()
    print(json.dumps(json.loads(result), indent=2))


def run_orchestrator_local(stop_doc: str) -> None:
    from utils.session import new_session_id
    from agents.orchestrator_agent import invoke
    session_id = new_session_id()
    print(f"Session ID: {session_id}")
    result = invoke({"stop_document": stop_doc, "session_id": session_id})
    print(json.dumps(result, indent=2))


def run_discovery_local(stop_doc: str) -> None:
    from agents.discovery_agent import invoke
    result = invoke({"stop_document": stop_doc, "role_arn": _require("DISCOVERY_ROLE_ARN")})
    print(json.dumps(result, indent=2))


def run_monitoring_local(s3_key: str = "", env_prefix: str = "") -> None:
    from agents.monitoring_agent import invoke
    result = invoke({"s3_key": s3_key, "env_prefix": env_prefix})
    print(json.dumps(result, indent=2))


def run_remediation_local(alarm_event_file: str) -> None:
    from agents.remediation_agent import invoke
    with open(alarm_event_file) as f:
        alarm_event = json.load(f)
    result = invoke({"alarm_event": alarm_event})
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Invoke an agent in the sentinel-strands pipeline")
    p.add_argument("--agent",
                   choices=["orchestrator", "discovery", "monitoring", "remediation"],
                   default="orchestrator")
    p.add_argument("--stop", default="samples/stop.json", help="STOP document path")
    p.add_argument("--s3-key", default="", help="Discovery S3 key (monitoring only)")
    p.add_argument("--env-prefix", default="", help="Env prefix filter (monitoring only)")
    p.add_argument("--alarm-event", default="samples/alarm_event.json",
                   help="Alarm event JSON file (remediation only)")
    args = p.parse_args()

    run_type = config.get("RUN_TYPE", "local").lower()

    if args.agent == "orchestrator":
        with open(args.stop) as f:
            stop_doc = f.read()
        if run_type == "agentcore":
            _invoke_agentcore("ORCHESTRATOR_AGENT_ARN", json.dumps({"stop_document": stop_doc}))
        else:
            run_orchestrator_local(stop_doc)

    elif args.agent == "discovery":
        with open(args.stop) as f:
            stop_doc = f.read()
        if run_type == "agentcore":
            _invoke_agentcore("DISCOVERY_AGENT_ARN", json.dumps({"stop_document": stop_doc,
                                                        "role_arn": _require("DISCOVERY_ROLE_ARN")}))
        else:
            run_discovery_local(stop_doc)

    elif args.agent == "monitoring":
        if run_type == "agentcore":
            _invoke_agentcore("MONITORING_AGENT_ARN", json.dumps({"s3_key": args.s3_key,
                                                                    "env_prefix": args.env_prefix}))
        else:
            run_monitoring_local(args.s3_key, args.env_prefix)

    elif args.agent == "remediation":
        if run_type == "agentcore":
            with open(args.alarm_event) as f:
                alarm_event = json.load(f)
            _invoke_agentcore("REMEDIATION_AGENT_ARN", json.dumps({"alarm_event": alarm_event}))
        else:
            run_remediation_local(args.alarm_event)
