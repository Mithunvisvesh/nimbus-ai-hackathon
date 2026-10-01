from typing import Tuple, List, Optional
from src.schemas.plan import CandidatePlan, PolicyOutcome, PlanStatus, RiskLevel
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
        self.mcp = mcp_server

    def evaluate(self, plan: CandidatePlan) -> PolicyEvaluationResult:
        # If no actions were generated
        if not plan.actions:
            return PolicyEvaluationResult(
                outcome=PolicyOutcome.CLARIFY,
                reason="No concrete actions could be resolved from request.",
            )

        # 1. Permission Verification (RBAC)
        if plan.user_role == "READ_ONLY":
            for act in plan.actions:
                # Any mutation attempted by READ_ONLY is blocked
                if not act.operation.endswith(".list") and not act.operation.endswith(".get") and not act.operation.endswith(".check"):
                    return PolicyEvaluationResult(
                        outcome=PolicyOutcome.BLOCK,
                        reason=f"Role 'READ_ONLY' is unauthorized to execute mutating operation '{act.operation}'.",
                    )

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
                                suggested_alternatives=["2026-10-02T16:00:00Z", "2026-10-02T18:00:00Z"],
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

            if act.risk_level in (RiskLevel.HIGH, RiskLevel.CRITICAL):
                return PolicyEvaluationResult(
                    outcome=PolicyOutcome.CONFIRM,
                    reason=f"High risk level ({act.risk_level.value}) declared for action '{act.action_id}'. Confirmation required.",
                )

            if not act.reversible:
                return PolicyEvaluationResult(
                    outcome=PolicyOutcome.CONFIRM,
                    reason=f"Irreversible action '{act.action_id}' requires explicit confirmation.",
                )

        # 4. Low-Risk Auto-Approval
        return PolicyEvaluationResult(
            outcome=PolicyOutcome.AUTO_APPROVE,
            reason="All actions are authorized, internal, reversible, and conflict-free.",
        )
