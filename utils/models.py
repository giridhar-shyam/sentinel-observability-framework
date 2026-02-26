"""Data models for the Cloud Discovery Agent."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Environment:
    name: str
    provider: str
    type: str
    regions: list[str] = field(default_factory=list)


@dataclass
class EntryPoint:
    type: str
    id: str
    region: str


@dataclass
class Hints:
    known_services: list[str] = field(default_factory=list)
    exclude_namespaces: list[str] = field(default_factory=list)
    tech_stack: list[str] = field(default_factory=list)


@dataclass
class AgentConfig:
    discovery_depth: str = "standard"   # shallow | standard | deep
    autonomy_level: str = "standard"    # supervised | standard | full
    max_resources_scanned: int | None = None
    dry_run: bool = False


@dataclass
class STOPDocument:
    stop_version: str
    environment: Environment
    entry_points: list[EntryPoint]
    hints: Hints = field(default_factory=Hints)
    agent_config: AgentConfig = field(default_factory=AgentConfig)


@dataclass
class ParseError:
    field: str
    reason: str
