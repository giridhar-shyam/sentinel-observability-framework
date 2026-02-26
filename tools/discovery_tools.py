"""AWS discovery tools — called by the Strands Agent during discovery."""
from __future__ import annotations
import json
import logging
from datetime import datetime, timezone
import boto3
from botocore.exceptions import ClientError
from strands import tool

logger = logging.getLogger(__name__)

# Tracks the S3 path written by write_discovery_result — read by discovery_agent.invoke()
_last_written_s3_path: list[str] = []


def _boto(service: str, credentials: dict, region: str):
    return boto3.client(
        service, region_name=region,
        aws_access_key_id=credentials["AccessKeyId"],
        aws_secret_access_key=credentials["SecretAccessKey"],
        aws_session_token=credentials["SessionToken"],
    )


def _safe(fn):
    try:
        return fn()
    except ClientError as e:
        code = e.response["Error"]["Code"]
        msg = e.response["Error"].get("Message", str(e))
        logger.warning("AWS error %s: %s", code, msg)
        return {"error": code, "reason": msg}


def _arn(service, region, account, resource):
    return f"arn:aws:{service}:{region}:{account}:{resource}"


@tool
def assume_discovery_role(role_arn: str) -> str:
    """Assume the IAM discovery role and return temporary credentials as JSON.
    Returns a JSON object with AccessKeyId, SecretAccessKey, SessionToken."""
    try:
        sts = boto3.client("sts")
        resp = sts.assume_role(RoleArn=role_arn, RoleSessionName="CloudDiscoveryAgent")
        return json.dumps(resp["Credentials"], default=str)
    except ClientError as e:
        return json.dumps({"error": e.response["Error"]["Code"],
                           "reason": e.response["Error"].get("Message", str(e))})


@tool
def enumerate_ec2(region: str, account_id: str, credentials_json: str) -> str:
    """Enumerate EC2 VPCs and instances in the given region."""
    creds = json.loads(credentials_json)
    ec2 = _boto("ec2", creds, region)
    resources = []

    vpcs = _safe(lambda: ec2.describe_vpcs())
    for vpc in (vpcs.get("Vpcs", []) if isinstance(vpcs, dict) and "Vpcs" in vpcs else []):
        name = next((t["Value"] for t in vpc.get("Tags", []) if t["Key"] == "Name"), vpc["VpcId"])
        resources.append({"id": _arn("ec2", region, account_id, f"vpc/{vpc['VpcId']}"),
                           "type": "AWS::EC2::VPC", "name": name, "region": region,
                           "attributes": {"cidr": vpc.get("CidrBlock", ""), "state": vpc.get("State", "")}})

    instances = _safe(lambda: ec2.describe_instances(
        Filters=[{"Name": "instance-state-name", "Values": ["running", "stopped"]}]
    ))
    for r in (instances.get("Reservations", []) if isinstance(instances, dict) else []):
        for i in r.get("Instances", []):
            name = next((t["Value"] for t in i.get("Tags", []) if t["Key"] == "Name"), i["InstanceId"])
            resources.append({"id": _arn("ec2", region, account_id, f"instance/{i['InstanceId']}"),
                               "type": "AWS::EC2::Instance", "name": name, "region": region,
                               "attributes": {"instance_type": i.get("InstanceType", ""),
                                              "state": i.get("State", {}).get("Name", ""),
                                              "vpc_id": i.get("VpcId", "")}})
    return json.dumps(resources)


@tool
def enumerate_ecs(region: str, account_id: str, credentials_json: str) -> str:
    """Enumerate ECS clusters in the given region."""
    creds = json.loads(credentials_json)
    ecs = _boto("ecs", creds, region)
    resp = _safe(lambda: ecs.list_clusters())
    resources = [{"id": arn, "type": "AWS::ECS::Cluster", "name": arn.split("/")[-1],
                  "region": region, "attributes": {}}
                 for arn in (resp.get("clusterArns", []) if isinstance(resp, dict) else [])]
    return json.dumps(resources)


@tool
def enumerate_rds(region: str, account_id: str, credentials_json: str) -> str:
    """Enumerate RDS DB instances in the given region."""
    creds = json.loads(credentials_json)
    rds = _boto("rds", creds, region)
    resp = _safe(lambda: rds.describe_db_instances())
    resources = [{"id": db.get("DBInstanceArn", _arn("rds", region, account_id, f"db:{db['DBInstanceIdentifier']}")),
                  "type": "AWS::RDS::DBInstance", "name": db["DBInstanceIdentifier"],
                  "region": region, "attributes": {"engine": db.get("Engine", ""), "status": db.get("DBInstanceStatus", "")}}
                 for db in (resp.get("DBInstances", []) if isinstance(resp, dict) else [])]
    return json.dumps(resources)


@tool
def enumerate_s3(region: str, account_id: str, credentials_json: str) -> str:
    """Enumerate S3 buckets (global, reported under given region)."""
    creds = json.loads(credentials_json)
    s3 = _boto("s3", creds, region)
    resp = _safe(lambda: s3.list_buckets())
    resources = [{"id": f"arn:aws:s3:::{b['Name']}", "type": "AWS::S3::Bucket",
                  "name": b["Name"], "region": region, "attributes": {}}
                 for b in (resp.get("Buckets", []) if isinstance(resp, dict) else [])]
    return json.dumps(resources)


@tool
def enumerate_lambda(region: str, account_id: str, credentials_json: str) -> str:
    """Enumerate Lambda functions in the given region."""
    creds = json.loads(credentials_json)
    lmb = _boto("lambda", creds, region)
    resp = _safe(lambda: lmb.list_functions())
    resources = [{"id": fn["FunctionArn"], "type": "AWS::Lambda::Function",
                  "name": fn["FunctionName"], "region": region,
                  "attributes": {"runtime": fn.get("Runtime", "")}}
                 for fn in (resp.get("Functions", []) if isinstance(resp, dict) else [])]
    return json.dumps(resources)


@tool
def enumerate_standard_services(region: str, account_id: str, credentials_json: str) -> str:
    """Enumerate SNS topics, SQS queues, DynamoDB tables, ElastiCache clusters."""
    creds = json.loads(credentials_json)
    resources = []

    for svc, method, key, builder in [
        ("sns", lambda c: c.list_topics(), "Topics",
         lambda t: {"id": t["TopicArn"], "type": "AWS::SNS::Topic",
                    "name": t["TopicArn"].split(":")[-1], "region": region, "attributes": {}}),
        ("sqs", lambda c: c.list_queues(), "QueueUrls",
         lambda u: {"id": u, "type": "AWS::SQS::Queue",
                    "name": u.split("/")[-1], "region": region, "attributes": {"url": u}}),
        ("dynamodb", lambda c: c.list_tables(), "TableNames",
         lambda n: {"id": _arn("dynamodb", region, account_id, f"table/{n}"),
                    "type": "AWS::DynamoDB::Table", "name": n, "region": region, "attributes": {}}),
    ]:
        client = _boto(svc, creds, region)
        resp = _safe(lambda c=client, m=method: m(c))
        for item in (resp.get(key, []) if isinstance(resp, dict) else []):
            resources.append(builder(item))

    ec = _boto("elasticache", creds, region)
    resp = _safe(lambda: ec.describe_cache_clusters())
    for c in (resp.get("CacheClusters", []) if isinstance(resp, dict) else []):
        resources.append({"id": _arn("elasticache", region, account_id, f"cluster:{c['CacheClusterId']}"),
                          "type": "AWS::ElastiCache::CacheCluster",
                          "name": c["CacheClusterId"], "region": region,
                          "attributes": {"engine": c.get("Engine", "")}})
    return json.dumps(resources)


@tool
def enumerate_deep_services(region: str, account_id: str, credentials_json: str) -> str:
    """Enumerate MSK clusters, API Gateway APIs, EKS clusters, OpenSearch domains."""
    creds = json.loads(credentials_json)
    resources = []

    try:
        msk = _boto("kafka", creds, region)
        resp = _safe(lambda: msk.list_clusters())
        for c in (resp.get("ClusterInfoList", []) if isinstance(resp, dict) else []):
            resources.append({"id": c["ClusterArn"], "type": "AWS::MSK::Cluster",
                              "name": c["ClusterName"], "region": region, "attributes": {}})
    except Exception:
        pass

    apigw = _boto("apigateway", creds, region)
    resp = _safe(lambda: apigw.get_rest_apis())
    for api in (resp.get("items", []) if isinstance(resp, dict) else []):
        resources.append({"id": _arn("apigateway", region, account_id, f"/restapis/{api['id']}"),
                          "type": "AWS::ApiGateway::RestApi",
                          "name": api.get("name", api["id"]), "region": region, "attributes": {}})

    eks = _boto("eks", creds, region)
    resp = _safe(lambda: eks.list_clusters())
    for name in (resp.get("clusters", []) if isinstance(resp, dict) else []):
        resources.append({"id": _arn("eks", region, account_id, f"cluster/{name}"),
                          "type": "AWS::EKS::Cluster", "name": name, "region": region, "attributes": {}})

    es = _boto("es", creds, region)
    resp = _safe(lambda: es.list_domain_names())
    for d in (resp.get("DomainNames", []) if isinstance(resp, dict) else []):
        resources.append({"id": _arn("es", region, account_id, f"domain/{d['DomainName']}"),
                          "type": "AWS::OpenSearchService::Domain",
                          "name": d["DomainName"], "region": region, "attributes": {}})

    return json.dumps(resources)


@tool
def write_discovery_result(result_json: str, bucket_name: str, s3_key: str, region: str) -> str:
    """Validate and write the final Resource Graph JSON to S3.
    result_json must be a complete, valid JSON string with 'metadata' and 'resources' keys.
    s3_key is the full key path (e.g. sessions/<id>/discovery/resource_graph.json).
    Returns 's3://<bucket>/<key>' on success, or a validation error message."""
    try:
        parsed = json.loads(result_json)
    except json.JSONDecodeError as e:
        return f"error: invalid JSON — {e}. Fix the JSON and call this tool again."

    missing = [k for k in ("metadata", "resources") if k not in parsed]
    if missing:
        return f"error: missing required keys {missing}. Fix and call again."

    if not isinstance(parsed.get("resources"), list):
        return "error: 'resources' must be a list. Fix and call again."

    try:
        boto3.client("s3", region_name=region).put_object(
            Bucket=bucket_name, Key=s3_key,
            Body=json.dumps(parsed, indent=2),
            ContentType="application/json",
        )
        s3_path = f"s3://{bucket_name}/{s3_key}"
        _last_written_s3_path.clear()
        _last_written_s3_path.append(s3_path)
        logger.info("Discovery results written to %s", s3_path)
        return s3_path
    except Exception as e:
        return f"error: S3 write failed — {e}"
