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
        events = self.mcp.list_calendar_events()
        if not events:
            return []

        # Case 1: Multi-event "Clear afternoon calendar" (Beat 2)
        if "afternoon" in intent.entities or "clear" in intent.goal.lower():
            afternoon_events = [
                e for e in events
                if any(t in e.get("start_time", "") for t in ["12:", "13:", "14:", "15:", "16:", "17:"])
            ]
            if not afternoon_events:
                afternoon_events = events

            actions = []
            for ev in afternoon_events:
                has_external = any(att.get("is_external", False) for att in ev.get("attendees", []))
                risk = RiskLevel.HIGH if has_external else RiskLevel.LOW
                before = dict(ev)

                actions.append(
                    PlannedAction(
                        action_id=f"act_{uuid.uuid4().hex[:6]}",
                        resource_type="calendar",
                        resource_id=ev["id"],
                        operation="calendar.update_event",
                        parameters={
                            "event_id": ev["id"],
                            "title": f"[CANCELLED] {ev.get('title', '')}",
                        },
                        before_state=before,
                        risk_level=risk,
                        reversible=True,
                        compensation_action=CompensationAction(
                            operation="calendar.update_event",
                            parameters={
                                "event_id": ev["id"],
                                "title": before.get("title", ""),
                                "start_time": before.get("start_time"),
                                "end_time": before.get("end_time"),
                            },
                        ),
                        constraints=intent.constraints,
                    )
                )
            return actions

        # Case 2: Target event reschedule (e.g. 3 PM sync)
        target_event = None
        for ev in events:
            start = ev.get("start_time", "")
            if "15:00" in start or "3pm" in ev.get("id", "") or "sync" in ev.get("title", "").lower():
                target_event = ev
                break

        if not target_event and events:
            target_event = events[0]

        if not target_event:
            return []

        # Determine target times
        target_start = "2026-10-02T16:00:00Z"
        target_end = "2026-10-02T16:30:00Z"

        # Check if intent asked for 5 PM
        for ent in intent.entities:
            if "5" in str(ent).lower():
                target_start = "2026-10-02T17:00:00Z"
                target_end = "2026-10-02T17:30:00Z"

        before_state = dict(target_event)
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
