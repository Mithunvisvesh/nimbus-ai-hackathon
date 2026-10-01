"""Pre-Execution Resource Freshness Checker for CARE.

Validates that target resources have not drifted or been modified by concurrent
human activity or external systems between read-only dry-run planning and tool execution.
"""

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from src.mcp.server import CareMCPServer


class FreshnessMismatch(BaseModel):
    key: str
    expected_before: Any
    actual_live: Any


class FreshnessResult(BaseModel):
    is_fresh: bool
    resource_id: str
    resource_type: str
    mismatches: List[FreshnessMismatch] = Field(default_factory=list)
    live_state: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None


class FreshnessChecker:
    """Pre-execution resource freshness validator."""

    def __init__(self, mcp_server: CareMCPServer):
        self.mcp = mcp_server

    def check_freshness(
        self,
        resource_type: str,
        resource_id: str,
        expected_before_state: Dict[str, Any],
        critical_keys: Optional[List[str]] = None,
    ) -> FreshnessResult:
        """Verify that live resource state matches the recorded dry-run before_state.

        Args:
            resource_type: The domain type (e.g. 'calendar', 'tickets').
            resource_id: The identifier of the resource.
            expected_before_state: State captured during dry-run read.
            critical_keys: Optional list of keys to strictly enforce. If omitted,
                           defaults to common domain critical keys.
        """
        live_state: Optional[Dict[str, Any]] = None

        if resource_type == "calendar":
            live_state = self.mcp.get_calendar_event(resource_id)
        elif resource_type == "tickets":
            live_state = self.mcp.get_ticket(resource_id) if hasattr(self.mcp, "get_ticket") else None
        else:
            live_state = None

        if live_state is None:
            return FreshnessResult(
                is_fresh=False,
                resource_id=resource_id,
                resource_type=resource_type,
                error_message=f"Resource '{resource_id}' no longer exists in live store.",
            )

        keys_to_check = critical_keys or list(set(expected_before_state) | set(live_state))

        mismatches: List[FreshnessMismatch] = []
        for key in keys_to_check:
            if key in expected_before_state:
                expected_val = expected_before_state[key]
                actual_val = live_state.get(key)
                if expected_val != actual_val:
                    mismatches.append(
                        FreshnessMismatch(
                            key=key,
                            expected_before=expected_val,
                            actual_live=actual_val,
                        )
                    )

        is_fresh = len(mismatches) == 0
        error_msg = None
        if not is_fresh:
            mismatch_str = "; ".join(
                f"{m.key}: expected {m.expected_before!r}, live {m.actual_live!r}"
                for m in mismatches
            )
            error_msg = f"STALE_RESOURCE_STATE: Resource '{resource_id}' modified. ({mismatch_str})"

        return FreshnessResult(
            is_fresh=is_fresh,
            resource_id=resource_id,
            resource_type=resource_type,
            mismatches=mismatches,
            live_state=live_state,
            error_message=error_msg,
        )
