import uuid
from typing import Optional, List, Dict, Any
from src.schemas.intent import StructuredIntent
from src.schemas.plan import (
    CandidatePlan,
    PlannedAction,
    CompensationAction,
    RiskLevel,
    PlanStatus,
)
from src.mcp.server import CareMCPServer
from src.integrity.hasher import compute_action_hash

class DryRunPlanner:
    """
    Read-Only Dry-Run Planner.
    Resolves concrete target resources and before_state without mutating system state.
    """
    def __init__(self, mcp_server: CareMCPServer):
        self.mcp = mcp_server

    def generate_candidate_plan(
        self,
        intent: StructuredIntent,
        actor: str = "user_mithun",
        user_role: str = "STANDARD_USER",
    ) -> CandidatePlan:
        plan_id = f"plan_{uuid.uuid4().hex[:12]}"
        actions: List[PlannedAction] = []

        if intent.scope == "calendar":
            actions = self._resolve_calendar_actions(intent)
        elif intent.scope == "tickets":
            actions = self._resolve_tickets_actions(intent)

        # Compute canonical action hash
        action_hash = compute_action_hash(actions) if actions else ""

        return CandidatePlan(
            plan_id=plan_id,
            intent=intent,
            actor=actor,
            user_role=user_role,
            actions=actions,
            action_hash=action_hash,
            status=PlanStatus.PLANNED,
        )

    def _resolve_calendar_actions(self, intent: StructuredIntent) -> List[PlannedAction]:
        # Query read-only calendar events
        events = self.mcp.list_calendar_events()

        # Target event matching (e.g. 3 PM sync)
        target_event = None
        for ev in events:
            # Check 3 PM / 15:00
            start = ev.get("start_time", "")
            if "15:00" in start or "3pm" in ev.get("id", "") or "sync" in ev.get("title", "").lower():
                target_event = ev
                break

        if not target_event and events:
            target_event = events[0]

        if not target_event:
            return []

        # Determine target times
        # For Phase 1 vertical slice: 3 PM (15:00) to 4 PM (16:00) or specified in entities
        target_start = "2026-10-02T16:00:00Z"
        target_end = "2026-10-02T16:30:00Z"

        # Check if intent asked for 5 PM
        for ent in intent.entities:
            if "5" in str(ent).lower():
                target_start = "2026-10-02T17:00:00Z"
                target_end = "2026-10-02T17:30:00Z"

        before_state = dict(target_event)

        # Check if external attendees exist
        has_external = any(att.get("is_external", False) for att in target_event.get("attendees", []))
        risk_level = RiskLevel.HIGH if has_external else RiskLevel.LOW

        action = PlannedAction(
            action_id=f"act_{uuid.uuid4().hex[:6]}",
            resource_type="calendar",
            resource_id=target_event["id"],
            operation="calendar.update_event",
            parameters={
                "event_id": target_event["id"],
                "start_time": target_start,
                "end_time": target_end,
            },
            before_state=before_state,
            risk_level=risk_level,
            reversible=True,
            compensation_action=CompensationAction(
                operation="calendar.update_event",
                parameters={
                    "event_id": target_event["id"],
                    "start_time": before_state.get("start_time"),
                    "end_time": before_state.get("end_time"),
                },
            ),
            constraints=intent.constraints,
        )
        return [action]

    def _resolve_tickets_actions(self, intent: StructuredIntent) -> List[PlannedAction]:
        # Placeholder for ticket domain (will be fully wired in Phase 3)
        return []
