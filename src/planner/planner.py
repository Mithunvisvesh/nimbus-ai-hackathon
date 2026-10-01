import uuid
import re
from datetime import datetime
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
                return []

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
        source_time = self._parse_clock(intent.entities[0]) if len(intent.entities) >= 2 else None
        candidates = [ev for ev in events if source_time and self._event_clock(ev) == source_time]
        if not candidates:
            name_entities = [
                ent for ent in intent.entities
                if ent.lower() not in {"meeting", "sync"}
                and self._parse_clock(ent) is None
                and self._parse_date(ent) is None
            ]
            candidates = [ev for ev in events if any(
                ent.lower() in ev.get("title", "").lower()
                for ent in name_entities
            )]
        if len(candidates) != 1:
            return []
        target_event = candidates[0]

        # Determine target times
        target_clock = self._parse_clock(intent.entities[1]) if len(intent.entities) >= 2 else None
        if target_clock is None:
            return []
        start = datetime.fromisoformat(target_event["start_time"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(target_event["end_time"].replace("Z", "+00:00"))
        target_start_dt = start.replace(hour=target_clock[0], minute=target_clock[1], second=0, microsecond=0)
        if len(intent.entities) >= 4:
            explicit_date = self._parse_date(intent.entities[3])
            if explicit_date is None:
                return []
            target_start_dt = target_start_dt.replace(
                year=explicit_date.year, month=explicit_date.month, day=explicit_date.day
            )
        duration = end - start
        target_end_dt = target_start_dt + duration
        target_start = target_start_dt.isoformat().replace("+00:00", "Z")
        target_end = target_end_dt.isoformat().replace("+00:00", "Z")

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
        tickets = self.mcp.list_tickets() if hasattr(self.mcp, "list_tickets") else []
        if not tickets:
            return []

        target_ticket = None
        for ent in intent.entities:
            ent_str = str(ent).lower().replace("-", "_")
            for t in tickets:
                if t["id"].lower() == ent_str or ent_str in t["id"].lower():
                    target_ticket = t
                    break
            if target_ticket:
                break

        if not target_ticket:
            matches = [t for t in tickets if any(
                len(word) >= 3 and word.lower() in t.get("title", "").lower()
                for word in intent.entities
            )]
            if len(matches) == 1:
                target_ticket = matches[0]

        if not target_ticket:
            return []

        before_state = dict(target_ticket)
        is_escalated = target_ticket.get("is_escalated", False) or "escalation" in target_ticket.get("tags", [])
        risk_level = RiskLevel.HIGH if is_escalated else RiskLevel.LOW

        new_status = "closed"
        if "reopen" in intent.goal.lower():
            new_status = "open"
        elif "in_progress" in intent.goal.lower():
            new_status = "in_progress"

        action = PlannedAction(
            action_id=f"act_{uuid.uuid4().hex[:6]}",
            resource_type="tickets",
            resource_id=target_ticket["id"],
            operation="tickets.update_status",
            parameters={
                "ticket_id": target_ticket["id"],
                "new_status": new_status,
                "resolution_notes": f"Automated update via CARE: {intent.goal}",
            },
            before_state=before_state,
            risk_level=risk_level,
            reversible=True,
            compensation_action=CompensationAction(
                operation="tickets.update_status",
                parameters={
                    "ticket_id": target_ticket["id"],
                    "new_status": before_state.get("status", "open"),
                    "resolution_notes": before_state.get("resolution_notes"),
                    "clear_resolution_notes": before_state.get("resolution_notes") is None,
                },
            ),
            constraints=intent.constraints,
        )
        return [action]

    @staticmethod
    def _parse_clock(value: str) -> Optional[tuple[int, int]]:
        match = re.fullmatch(r"\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*", value.lower())
        if not match:
            return None
        hour, minute = int(match.group(1)), int(match.group(2) or 0)
        meridiem = match.group(3)
        if minute > 59 or hour > (12 if meridiem else 23) or hour < (1 if meridiem else 0):
            return None
        if meridiem:
            hour = hour % 12 + (12 if meridiem == "pm" else 0)
        return hour, minute

    @classmethod
    def _event_clock(cls, event: Dict[str, Any]) -> Optional[tuple[int, int]]:
        try:
            value = datetime.fromisoformat(event["start_time"].replace("Z", "+00:00"))
            return value.hour, value.minute
        except (KeyError, ValueError):
            return None

    @staticmethod
    def _parse_date(value: str):
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            return None
