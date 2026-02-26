"""STOP Document parser."""
from __future__ import annotations
import json
from utils.models import STOPDocument, Environment, EntryPoint, Hints, AgentConfig, ParseError

VALID_PROVIDERS = {"aws", "gcp", "azure", "on-prem", "hybrid"}
VALID_ENV_TYPES = {"production", "staging", "dev", "test"}


class STOPParser:
    def parse(self, path: str) -> tuple[STOPDocument | None, ParseError | None]:
        try:
            with open(path, encoding="utf-8") as f:
                return self.parse_from_string(f.read())
        except OSError as e:
            return None, ParseError(field="path", reason=str(e))

    def parse_from_string(self, json_str: str) -> tuple[STOPDocument | None, ParseError | None]:
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as e:
            return None, ParseError(field="document", reason=f"invalid JSON: {e}")

        if data.get("stop_version") != "1.0":
            return None, ParseError(field="stop_version", reason=f"expected '1.0', got {data.get('stop_version')!r}")

        env = data.get("environment", {})
        if env.get("provider") not in VALID_PROVIDERS:
            return None, ParseError(field="environment.provider", reason=f"must be one of {sorted(VALID_PROVIDERS)}")
        if env.get("type") not in VALID_ENV_TYPES:
            return None, ParseError(field="environment.type", reason=f"must be one of {sorted(VALID_ENV_TYPES)}")

        eps = data.get("entry_points", [])
        if not eps:
            return None, ParseError(field="entry_points", reason="at least one entry point is required")

        hints_data = data.get("hints", {})
        cfg_data = data.get("agent_config", {})

        return STOPDocument(
            stop_version=data["stop_version"],
            environment=Environment(
                name=env.get("name", ""),
                provider=env["provider"],
                type=env["type"],
                regions=env.get("regions", []),
            ),
            entry_points=[EntryPoint(type=ep.get("type", ""), id=ep.get("id", ""), region=ep.get("region", "")) for ep in eps],
            hints=Hints(
                known_services=hints_data.get("known_services", []),
                exclude_namespaces=hints_data.get("exclude_namespaces", []),
                tech_stack=hints_data.get("tech_stack", []),
            ),
            agent_config=AgentConfig(
                discovery_depth=cfg_data.get("discovery_depth", "standard"),
                autonomy_level=cfg_data.get("autonomy_level", "standard"),
                max_resources_scanned=cfg_data.get("max_resources_scanned"),
                dry_run=cfg_data.get("dry_run", False),
            ),
        ), None
