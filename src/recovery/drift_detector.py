"""Pre-Compensation Drift Detector for CARE.

Enforces ADR-008 and Invariant Rule 8:
Before executing any saga compensation action, the system must assert:
    current_live_state == journaled after_state

If an external human or third-party service modified the resource post-execution,
automatic rollback is immediately HALTED and flagged for human review to prevent
blind overwrites of legitimate newer modifications.
"""

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from src.mcp.server import CareMCPServer
from src.journal.db import ActionJournalDB


class DriftResult(BaseModel):
    drift_detected: bool
    resource_id: str
    resource_type: str
    expected_after_state: Dict[str, Any]
    current_live_state: Optional[Dict[str, Any]] = None
    differences: List[str] = Field(default_factory=list)
    incident_id: Optional[int] = None
    message: str = ""


class DriftDetector:
    """Detects post-execution state changes prior to saga compensation."""

    def __init__(self, mcp_server: CareMCPServer, journal_db: Optional[ActionJournalDB] = None):
        self.mcp = mcp_server
        self.journal = journal_db

    def check_drift(
        self,
        resource_type: str,
        resource_id: str,
        expected_after_state: Dict[str, Any],
        plan_id: Optional[str] = None,
        action_id: Optional[str] = None,
        critical_keys: Optional[List[str]] = None,
    ) -> DriftResult:
        """Compare current live resource state against the journaled after_state.

        Returns:
            DriftResult indicating whether external drift occurred.
        """
        # Fetch live state from MCP server
        live_state: Optional[Dict[str, Any]] = None
        if resource_type == "calendar":
            live_state = self.mcp.get_calendar_event(resource_id)
        elif resource_type == "tickets":
            live_state = self.mcp.get_ticket(resource_id) if hasattr(self.mcp, "get_ticket") else None

        if live_state is None:
            # Resource disappeared externally
            diffs = [f"Resource '{resource_id}' no longer exists in live storage."]
            incident_id = None
            if self.journal and plan_id and action_id:
                incident_id = self.journal.log_drift_incident(
                    plan_id=plan_id,
                    action_id=action_id,
                    resource_id=resource_id,
                    expected_after_state=expected_after_state,
                    observed_drift_state={"status": "missing"},
                    notes="Resource deleted externally prior to compensation",
                )
            return DriftResult(
                drift_detected=True,
                resource_id=resource_id,
                resource_type=resource_type,
                expected_after_state=expected_after_state,
                current_live_state=None,
                differences=diffs,
                incident_id=incident_id,
                message=f"DRIFT_DETECTED: Resource '{resource_id}' was deleted externally.",
            )

        # Check key fields for differences
        keys_to_compare = critical_keys or list(set(expected_after_state) | set(live_state))

        differences = []
        for key in keys_to_compare:
            exp_val = expected_after_state.get(key)
            live_val = live_state.get(key)
            if exp_val != live_val:
                differences.append(
                    f"Field '{key}': expected {exp_val!r} (journaled), found {live_val!r} (live)"
                )

        has_drift = len(differences) > 0
        incident_id = None
        message = "No drift detected: live state matches journaled after_state."

        if has_drift:
            message = (
                f"DRIFT_DETECTED: Resource '{resource_id}' was modified externally after execution. "
                f"Differences: {'; '.join(differences)}. Rollback aborted to protect human edits."
            )
            if self.journal and plan_id and action_id:
                incident_id = self.journal.log_drift_incident(
                    plan_id=plan_id,
                    action_id=action_id,
                    resource_id=resource_id,
                    expected_after_state=expected_after_state,
                    observed_drift_state=live_state,
                    notes=message,
                )

        return DriftResult(
            drift_detected=has_drift,
            resource_id=resource_id,
            resource_type=resource_type,
            expected_after_state=expected_after_state,
            current_live_state=live_state,
            differences=differences,
            incident_id=incident_id,
            message=message,
        )
