"""Saga Compensation Runner for CARE.

Implements ADR-007 and ADR-008:
Executes saga-style reverse compensation for completed actions upon multi-step plan failure.
Asserts pre-compensation drift check before every rollback operation:
  - If live state == journaled after_state: rollback proceeds safely.
  - If live state != journaled after_state (drift): rollback halts immediately,
    the action is marked 'drift', and an incident is logged for human review.
"""

from typing import Any, Dict, List, Optional
import secrets
from pydantic import BaseModel, Field

from src.schemas.plan import PlanStatus
from src.schemas.journal import ActionJournalStatus, JournalRecord
from src.journal.db import ActionJournalDB
from src.mcp.server import CareMCPServer
from src.recovery.drift_detector import DriftDetector, DriftResult
from src.mcp.fastmcp_server import create_fastmcp_server, call_fastmcp_tool_sync


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

    def __init__(self, journal_db: ActionJournalDB, mcp_server: CareMCPServer, mcp_protocol=None):
        self.journal = journal_db
        self.mcp = mcp_server
        self.mcp_protocol = mcp_protocol or create_fastmcp_server(mcp_server, journal_db)
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

        # Reconcile actions interrupted after their write-ahead record but before
        # the post-action journal update. Never compensate around an unknown result.
        inflight = [r for r in records if r.status in (ActionJournalStatus.EXECUTING, ActionJournalStatus.UNKNOWN)]
        if inflight and self._reconcile_inflight(plan_id, inflight):
            return SagaCompensationResult(
                plan_id=plan_id,
                status="drift",
                message="An interrupted action could not be reconciled safely; compensation halted for human review.",
            )
        records = self.journal.get_journal_records(plan_id)

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
                dispatch_token = secrets.token_urlsafe(32)
                self.journal.prepare_compensation_dispatch(plan_id, record.action_id, dispatch_token)
                comp_result = call_fastmcp_tool_sync(
                    self.mcp_protocol,
                    comp_op,
                    {
                        "plan_id": plan_id,
                        "action_hash": record.action_hash,
                        "action_id": record.action_id,
                        "dispatch_token": dispatch_token,
                        **comp_params,
                        "dispatch_kind": "compensate",
                    },
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

    def _reconcile_inflight(self, plan_id: str, records: List[JournalRecord]) -> bool:
        """Resolve write-ahead-only records by comparing live state to before/expected-after."""
        plan = self.journal.get_plan(plan_id)
        actions = {a["action_id"]: a for a in (plan or {}).get("actions", [])}
        for record in records:
            action = actions.get(record.action_id)
            live = self._get_live_state(record.resource_type, record.resource_id)
            expected = self._predict_after_state(record.before_state, action or {})
            with self.journal._get_connection() as conn:
                row = conn.execute(
                    "SELECT record_id FROM journal_records WHERE plan_id = ? AND action_id = ?",
                    (plan_id, record.action_id),
                ).fetchone()
            if not row:
                raise RuntimeError(f"Missing journal row for in-flight action {record.action_id}.")
            if live == record.before_state:
                self.journal.update_action_status(row["record_id"], ActionJournalStatus.FAILED)
            elif expected is not None and live == expected:
                self.journal.log_post_action_success(row["record_id"], after_state=live, result=live)
            else:
                self.journal.log_drift_incident(
                    plan_id=plan_id,
                    action_id=record.action_id,
                    resource_id=record.resource_id,
                    expected_after_state=expected or {},
                    observed_drift_state=live or {"status": "missing"},
                    notes="Interrupted action state matches neither its before_state nor predicted after_state.",
                )
                self.journal.update_action_status(row["record_id"], ActionJournalStatus.DRIFT)
                self.journal.update_plan_status(plan_id, PlanStatus.DRIFT)
                return True
        return False

    def _get_live_state(self, resource_type: str, resource_id: str) -> Optional[Dict[str, Any]]:
        if resource_type == "calendar":
            return self.mcp.get_calendar_event(resource_id)
        if resource_type == "tickets":
            return self.mcp.get_ticket(resource_id)
        return None

    @staticmethod
    def _predict_after_state(before: Dict[str, Any], action: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not action:
            return None
        expected = dict(before)
        params = action.get("parameters", {})
        operation = action.get("operation")
        if operation == "calendar.update_event":
            for key in ("start_time", "end_time", "title"):
                if params.get(key) is not None:
                    expected[key] = params[key]
            return expected
        if operation == "tickets.update_status":
            expected["status"] = params.get("new_status", params.get("status", "closed"))
            if params.get("clear_resolution_notes"):
                expected.pop("resolution_notes", None)
            elif params.get("resolution_notes") is not None:
                expected["resolution_notes"] = params["resolution_notes"]
            return expected
        return None
