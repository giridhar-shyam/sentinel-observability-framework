"""Session management — generates session IDs and S3 path helpers for the workarea bucket."""
from __future__ import annotations
import uuid
from datetime import datetime, timezone


def new_session_id() -> str:
    """Generate a unique session ID: YYYYMMDDTHHMMSSZ-<8-char-uuid>."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{ts}-{uuid.uuid4().hex[:8]}"


class SessionPaths:
    """S3 key helpers for a given session within the workarea bucket."""

    def __init__(self, bucket: str, session_id: str) -> None:
        self.bucket = bucket
        self.session_id = session_id
        self._base = f"sessions/{session_id}"

    # ── Per-agent output keys ────────────────────────────────────────────

    @property
    def stop_doc(self) -> str:
        return f"{self._base}/input/stop.json"

    @property
    def discovery_output(self) -> str:
        return f"{self._base}/discovery/resource_graph.json"

    @property
    def monitoring_output(self) -> str:
        return f"{self._base}/monitoring/alarm_audit.json"

    @property
    def orchestrator_summary(self) -> str:
        return f"{self._base}/orchestrator/run_summary.json"

    # ── S3 URI helpers ───────────────────────────────────────────────────

    def s3_uri(self, key: str) -> str:
        return f"s3://{self.bucket}/{key}"

    @property
    def discovery_s3_uri(self) -> str:
        return self.s3_uri(self.discovery_output)

    @property
    def monitoring_s3_uri(self) -> str:
        return self.s3_uri(self.monitoring_output)

    def remediation_output(self, alarm_name: str) -> str:
        safe = alarm_name.replace("/", "_").replace(" ", "_")
        return f"{self._base}/remediation/{safe}.json"
