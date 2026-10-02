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
            actions = self._resolve_calendar_actions(intent, actor=actor)
        elif intent.scope == "tickets":
            actions = self._resolve_tickets_actions(intent, actor=actor)

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

    def _resolve_calendar_actions(self, intent: StructuredIntent, actor: str = "user_mithun") -> List[PlannedAction]:
        events = self.mcp.list_calendar_events()

        # Case 1: Multi-event "Clear afternoon calendar" (Beat 2)
        if "afternoon" in intent.entities or "clear" in intent.goal.lower():
            if not events:
                return []
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

        # Case 2: Calendar Create Event (e.g. "Schedule meeting with Alice at 4 PM")
        if "create" in intent.entities or any(w in intent.goal.lower() for w in ["schedule", "book", "add meeting", "create meeting"]):
            event_id = f"evt_{uuid.uuid4().hex[:8]}"
            title = "New Meeting"
            clock_tuple = (14, 0)

            # Extract title and clock from entities
            for ent in intent.entities:
                if ent == "create":
                    continue
                parsed_c = self._parse_clock(ent)
                if parsed_c:
                    clock_tuple = parsed_c
                elif len(ent) > 2 and title == "New Meeting":
                    title = ent

            if title == "New Meeting" and "meeting" in intent.goal.lower():
                title = intent.goal

            # Generate target ISO timestamps
            from datetime import timedelta
            base_date = datetime.now().date()
            if events:
                try:
                    first_ev_start = events[0].get("start_time", "")
                    if len(first_ev_start) >= 10:
                        base_date = datetime.strptime(first_ev_start[:10], "%Y-%m-%d").date()
                except Exception:
                    pass

            is_tomorrow = "tomorrow" in intent.entities or "tomorrow" in intent.goal.lower()
            target_date = (base_date + timedelta(days=1)) if is_tomorrow else base_date
            target_start_dt = datetime.combine(target_date, datetime.min.time()).replace(
                hour=clock_tuple[0], minute=clock_tuple[1], second=0, microsecond=0
            )
            target_end_dt = target_start_dt + timedelta(minutes=30)
            target_start = target_start_dt.isoformat() + "Z"
            target_end = target_end_dt.isoformat() + "Z"

            action = PlannedAction(
                action_id=f"act_{uuid.uuid4().hex[:6]}",
                resource_type="calendar",
                resource_id=event_id,
                operation="calendar.create_event",
                parameters={
                    "event_id": event_id,
                    "title": title,
                    "start_time": target_start,
                    "end_time": target_end,
                    "attendees": [{"name": actor, "is_external": False}],
                },
                before_state={},
                risk_level=RiskLevel.LOW,
                reversible=True,
                compensation_action=CompensationAction(
                    operation="calendar.delete_event",
                    parameters={"event_id": event_id},
                ),
                constraints=intent.constraints,
            )
            return [action]

        # Case 3: Cancel / Delete Single Event (e.g. "Cancel my 10 AM meeting", "Delete evt_001")
        if "cancel" in intent.entities or any(w in intent.goal.lower() for w in ["cancel", "delete", "remove", "drop"]):
            if not events:
                return []
            target_event = None

            # Try matching by ID, clock, title, or attendee
            for ent in intent.entities:
                if ent == "cancel":
                    continue
                # By ID
                for ev in events:
                    if ev["id"].lower() == ent.lower() or ent.lower() in ev["id"].lower():
                        target_event = ev
                        break
                if target_event:
                    break

                # By clock
                c = self._parse_clock(ent)
                if not c:
                    clk_m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", ent.lower())
                    if clk_m:
                        c = self._parse_clock(clk_m.group(0))
                if c:
                    for ev in events:
                        if self._event_clock(ev) == c:
                            target_event = ev
                            break
                if target_event:
                    break

                # By title or attendee substring
                for ev in events:
                    if ent.lower() in ev.get("title", "").lower() or any(
                        ent.lower() in att.get("name", "").lower() for att in ev.get("attendees", [])
                    ):
                        target_event = ev
                        break
                if target_event:
                    break

            if not target_event:
                # Check goal string for clock or words
                for ev in events:
                    ev_c = self._event_clock(ev)
                    if ev_c and f"{ev_c[0]}" in intent.goal:
                        target_event = ev
                        break
                    if any(w in ev.get("title", "").lower() for w in intent.goal.lower().split() if len(w) >= 4):
                        target_event = ev
                        break

            if not target_event:
                return []

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
                    "title": f"[CANCELLED] {target_event.get('title', '')}",
                },
                before_state=before_state,
                risk_level=risk_level,
                reversible=True,
                compensation_action=CompensationAction(
                    operation="calendar.update_event",
                    parameters={
                        "event_id": target_event["id"],
                        "title": before_state.get("title", ""),
                        "start_time": before_state.get("start_time"),
                        "end_time": before_state.get("end_time"),
                    },
                ),
                constraints=intent.constraints,
            )
            return [action]

        # Case 4: Target event reschedule (e.g. 3 PM sync to 4 PM)
        if not events:
            return []
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
            for ent in intent.entities:
                c = self._parse_clock(ent)
                if c is not None and c != source_time:
                    target_clock = c
                    break
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

    def _resolve_tickets_actions(self, intent: StructuredIntent, actor: str = "user_mithun") -> List[PlannedAction]:
        tickets = self.mcp.list_tickets() if hasattr(self.mcp, "list_tickets") else []

        # Case 1: Ticket Creation (e.g. "Create ticket for payment error", "File bug login failure")
        if "create" in intent.entities or any(w in intent.goal.lower() for w in ["create ticket", "file ticket", "file bug", "open ticket", "report bug"]):
            ticket_id = f"tkt_{uuid.uuid4().hex[:4]}"
            title = "New Bug Ticket"
            priority = "medium"

            for ent in intent.entities:
                if ent == "create":
                    continue
                if ent in ["high", "medium", "low", "critical"]:
                    priority = ent
                elif len(ent) > 3 and title == "New Bug Ticket":
                    title = ent

            if title == "New Bug Ticket" and "ticket" in intent.goal.lower():
                title = intent.goal

            action = PlannedAction(
                action_id=f"act_{uuid.uuid4().hex[:6]}",
                resource_type="tickets",
                resource_id=ticket_id,
                operation="tickets.create_ticket",
                parameters={
                    "ticket_id": ticket_id,
                    "title": title,
                    "priority": priority,
                    "assigned_to": actor,
                    "status": "open",
                },
                before_state={},
                risk_level=RiskLevel.HIGH if priority in ["high", "critical"] else RiskLevel.LOW,
                reversible=True,
                compensation_action=CompensationAction(
                    operation="tickets.delete_ticket",
                    parameters={"ticket_id": ticket_id},
                ),
                constraints=intent.constraints,
            )
            return [action]

        # Case 2: Ticket Status Mutation (close, reopen, in_progress)
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
        if "reopen" in intent.goal.lower() or "reopen" in intent.entities:
            new_status = "open"
        elif "in_progress" in intent.goal.lower() or "in_progress" in intent.entities:
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
