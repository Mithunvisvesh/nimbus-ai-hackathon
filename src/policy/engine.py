from typing import List, Optional
from src.schemas.plan import CandidatePlan, PolicyOutcome
from src.mcp.server import CareMCPServer

class PolicyEvaluationResult:
    def __init__(
        self,
        outcome: PolicyOutcome,
        reason: str,
        suggested_alternatives: Optional[List[str]] = None,
    ):
        self.outcome = outcome
        self.reason = reason
        self.suggested_alternatives = suggested_alternatives or []

class PolicyEngine:
    """
    Deterministic Policy & Risk Engine.
    Evaluates concrete actions against observable facts: RBAC, ambiguity, conflicts, risk, and reversibility.
    Precedence: Permissions -> Ambiguity/Feasibility -> Consequential Action -> Auto-Approval.
    """
    def __init__(self, mcp_server: Optional[CareMCPServer] = None):
        self.mcp = mcp_server or CareMCPServer()

    def evaluate(self, plan: CandidatePlan) -> PolicyEvaluationResult:
        # If no actions were generated
        if not plan.actions:
            return PolicyEvaluationResult(
                outcome=PolicyOutcome.CLARIFY,
                reason="No concrete actions could be resolved from request.",
            )

        # 1. Permission and trusted tool metadata verification (RBAC)
        for act in plan.actions:
            meta = self.mcp.get_tool_metadata(act.operation) if self.mcp else None
            if meta is None:
                return PolicyEvaluationResult(PolicyOutcome.BLOCK, f"Operation '{act.operation}' is not registered.")
            if not meta.read_only and plan.user_role == "READ_ONLY":
                return PolicyEvaluationResult(PolicyOutcome.BLOCK, f"Role 'READ_ONLY' is unauthorized to execute mutating operation '{act.operation}'.")

        # 2. Ambiguity & Feasibility Verification
        if plan.intent.ambiguities:
            return PolicyEvaluationResult(
                outcome=PolicyOutcome.CLARIFY,
                reason=f"Unresolved ambiguities detected: {'; '.join(plan.intent.ambiguities)}",
            )

        # Check calendar availability/conflict feasibility if MCP server is available
        if self.mcp:
            for act in plan.actions:
                if act.operation == "calendar.update_event":
                    target_start = act.parameters.get("start_time")
                    target_end = act.parameters.get("end_time")
                    if target_start and target_end:
                        avail = self.mcp.check_calendar_availability(
                            target_start, target_end, exclude_event_id=act.resource_id
                        )
                        if not avail.get("available", True):
                            conflict = avail.get("conflicting_event", {})
                            return PolicyEvaluationResult(
                                outcome=PolicyOutcome.CLARIFY,
                                reason=f"Target slot is occupied by '{conflict.get('title', 'another event')}'.",
                            suggested_alternatives=self._suggest_alternatives(target_start, target_end, act.resource_id),
                            )

        # 3. Consequential / External Actions
        for act in plan.actions:
            # Check for escalated tickets
            if act.before_state.get("is_escalated") or "escalation" in act.before_state.get("tags", []):
                return PolicyEvaluationResult(
                    outcome=PolicyOutcome.CONFIRM,
                    reason=f"Ticket '{act.resource_id}' is an active escalation. Explicit confirmation required before modification.",
                )

            # Check for external attendees
            attendees = act.before_state.get("attendees", [])
            has_external = any(att.get("is_external", False) for att in attendees)
            if has_external:
                return PolicyEvaluationResult(
                    outcome=PolicyOutcome.CONFIRM,
                    reason=f"Action affects external attendees on resource '{act.resource_id}'. Explicit user confirmation required.",
                )

            meta = self.mcp.get_tool_metadata(act.operation) if self.mcp else None
            if meta and (meta.external_effect or meta.affects_external_party or meta.destructive or not meta.reversible or not meta.compensation_supported):
                return PolicyEvaluationResult(
                    outcome=PolicyOutcome.CONFIRM,
                    reason=f"Operation '{act.operation}' has consequential effects or lacks safe compensation.",
                )

        # 4. Low-Risk Auto-Approval
        return PolicyEvaluationResult(
            outcome=PolicyOutcome.AUTO_APPROVE,
            reason="All actions are authorized, internal, reversible, and conflict-free.",
        )

    def _suggest_alternatives(self, start: str, end: str, exclude_event_id: str) -> List[str]:
        from datetime import datetime, timedelta
        try:
            start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return []
        duration = end_dt - start_dt
        candidates = []
        for hour in sorted(range(8, 19), key=lambda value: (abs(value - start_dt.hour), value)):
            slot = start_dt.replace(hour=hour, minute=0, second=0, microsecond=0)
            slot_end = slot + duration
            if slot == start_dt:
                continue
            if self.mcp.check_calendar_availability(
                slot.isoformat().replace("+00:00", "Z"),
                slot_end.isoformat().replace("+00:00", "Z"),
                exclude_event_id=exclude_event_id,
            ).get("available", False):
                candidates.append(slot.isoformat().replace("+00:00", "Z"))
                if len(candidates) == 2:
                    break
        return candidates
