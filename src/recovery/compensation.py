"""Saga Compensation Runner for CARE.

Implements ADR-007 and ADR-008:
Executes saga-style reverse compensation for completed actions upon multi-step plan failure.
Asserts pre-compensation drift check before every rollback operation:
  - If live state == journaled after_state: rollback proceeds safely.
  - If live state != journaled after_state (drift): rollback halts immediately,
    the action is marked 'drift', and an incident is logged for human review.
"""

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from src.schemas.plan import PlanStatus
from src.schemas.journal import ActionJournalStatus, JournalRecord
from src.journal.db import ActionJournalDB
from src.mcp.server import CareMCPServer
from src.recovery.drift_detector import DriftDetector, DriftResult


class CompensatedStep(BaseModel):
    action_id: str
    resource_id: str
    operation: str
    parameters: Dict[str, Any]
    status: str
    result: Optional[Dict[str, Any]] = None


class SagaCompensationResult(BaseModel):
    plan_id: str
    status: str  # "compensated" | "drift" | "no_actions_to_compensate" | "error"
    compensated_steps: List[CompensatedStep] = Field(default_factory=list)
    drift_incident: Optional[DriftResult] = None
    message: str = ""


class SagaCompensationRunner:
    """Executes backward saga compensation with drift protection."""

    def __init__(self, journal_db: ActionJournalDB, mcp_server: CareMCPServer):
        self.journal = journal_db
        self.mcp = mcp_server
        self.drift_detector = DriftDetector(mcp_server=mcp_server, journal_db=journal_db)

    def compensate_plan(
        self,
        plan_id: str,
        failed_action_index: Optional[int] = None,
    ) -> SagaCompensationResult:
        """Trigger reverse saga compensation for all completed actions in a plan.

        Args:
            plan_id: The ID of the failed plan.
            failed_action_index: Optional index of the failing action. If omitted,
                                 compensates all journal records marked 'done'.

        Returns:
            SagaCompensationResult documenting compensated steps or drift abort.
        """
        # 1. Fetch journal records for this plan
        records = self.journal.get_journal_records(plan_id)
        if not records:
            return SagaCompensationResult(
                plan_id=plan_id,
                status="no_actions_to_compensate",
                message="No journal records found for plan.",
            )

        # 2. Identify records eligible for compensation (status == 'done') in reverse order
        done_records = [r for r in records if r.status == ActionJournalStatus.DONE]
        done_records.reverse()

        if not done_records:
            return SagaCompensationResult(
                plan_id=plan_id,
                status="no_actions_to_compensate",
                message="No successfully completed actions require compensation.",
            )

        # Transition plan to RECOVERING
        self.journal.update_plan_status(plan_id, PlanStatus.RECOVERING)

        compensated_steps: List[CompensatedStep] = []

        for record in done_records:
            if not record.compensation_action:
                # Action does not declare a compensation action (e.g. irreversible)
                continue

            comp_op = record.compensation_action.get("operation")
            comp_params = record.compensation_action.get("parameters", {})

            # 3. Mandatory Pre-Compensation Drift Check (ADR-008 & Invariant Rule 8)
            drift_res = self.drift_detector.check_drift(
                resource_type=record.resource_type,
                resource_id=record.resource_id,
                expected_after_state=record.after_state or {},
                plan_id=plan_id,
                action_id=record.action_id,
            )

            if drift_res.drift_detected:
                # HALT compensation immediately
                self.journal.update_plan_status(plan_id, PlanStatus.DRIFT)
                # Find matching record in db to update status
                with self.journal._get_connection() as conn:
                    conn.execute(
                        "UPDATE journal_records SET status = ? WHERE plan_id = ? AND action_id = ?",
                        (ActionJournalStatus.DRIFT.value, plan_id, record.action_id),
                    )

                return SagaCompensationResult(
                    plan_id=plan_id,
                    status="drift",
                    compensated_steps=compensated_steps,
                    drift_incident=drift_res,
                    message=f"Compensation halted due to external drift on resource '{record.resource_id}'. Human review flagged.",
                )

            # 4. Dispatch compensation action via MCP
            try:
                comp_result = self.mcp.dispatch_tool(
                    operation=comp_op,
                    parameters=comp_params,
                    plan_id=plan_id,
                    action_hash=record.action_hash,
                )

                # 5. Mark journal record as COMPENSATED
                with self.journal._get_connection() as conn:
                    conn.execute(
                        "UPDATE journal_records SET status = ? WHERE plan_id = ? AND action_id = ?",
                        (ActionJournalStatus.COMPENSATED.value, plan_id, record.action_id),
                    )

                compensated_steps.append(
                    CompensatedStep(
                        action_id=record.action_id,
                        resource_id=record.resource_id,
                        operation=comp_op,
                        parameters=comp_params,
                        status="compensated",
                        result=comp_result,
                    )
                )

            except Exception as exc:
                return SagaCompensationResult(
                    plan_id=plan_id,
                    status="error",
                    compensated_steps=compensated_steps,
                    message=f"Error executing compensation for action '{record.action_id}': {exc}",
                )

        # 6. All eligible actions successfully compensated
        self.journal.update_plan_status(plan_id, PlanStatus.COMPENSATED)

        return SagaCompensationResult(
            plan_id=plan_id,
            status="compensated",
            compensated_steps=compensated_steps,
            message=f"Successfully compensated {len(compensated_steps)} actions in reverse order.",
        )
