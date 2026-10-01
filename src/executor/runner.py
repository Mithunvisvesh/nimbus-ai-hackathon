from typing import Any, Dict, List, Optional
from src.schemas.plan import PlanStatus
from src.schemas.journal import ActionJournalStatus
from src.journal.db import ActionJournalDB
from src.mcp.server import CareMCPServer
from src.integrity.freshness import FreshnessChecker
from src.verifier.invariant_checker import InvariantChecker
from src.recovery.compensation import SagaCompensationRunner, SagaCompensationResult
from src.integrity.hasher import compute_action_hash
from src.schemas.plan import PlannedAction, PolicyOutcome, CandidatePlan
from src.schemas.intent import StructuredIntent
from src.policy.engine import PolicyEngine


class ControlledExecutor:
    """Deterministic Controlled Executor.

    Enforces:
      1. Plan existence and APPROVED status check.
      2. Single-use replay protection via atomic CAS to EXECUTING.
      3. Cryptographic action_hash mismatch detection.
      4. Pre-execution per-action state freshness check (via FreshnessChecker).
      5. Write-ahead logging (status='executing' before tool call, status='done' after).
      6. Post-execution invariant verification (via InvariantChecker).
      7. Saga-style reverse compensation upon partial failure (via SagaCompensationRunner).
    """

    def __init__(self, journal_db: ActionJournalDB, mcp_server: CareMCPServer):
        self.journal = journal_db
        self.mcp = mcp_server
        self.freshness_checker = FreshnessChecker(mcp_server=mcp_server)
        self.compensation_runner = SagaCompensationRunner(
            journal_db=journal_db, mcp_server=mcp_server
        )

    def execute_plan(
        self,
        plan_id: str,
        submitted_action_hash: str,
        simulate_failure_at_action_index: Optional[int] = None,
        auto_compensate_on_failure: bool = True,
    ) -> Dict[str, Any]:
        # 1. Fetch stored plan from durable store
        stored_plan = self.journal.get_plan(plan_id)
        if not stored_plan:
            raise KeyError(f"Plan '{plan_id}' does not exist in durable store.")

        # 2. Verify stored status is APPROVED
        if stored_plan["status"] != PlanStatus.APPROVED.value:
            if stored_plan["status"] in (
                PlanStatus.EXECUTING.value,
                PlanStatus.DONE.value,
                PlanStatus.COMPENSATED.value,
                PlanStatus.DRIFT.value,
            ):
                raise PermissionError(
                    f"PLAN_ALREADY_CONSUMED: Plan '{plan_id}' is already {stored_plan['status']}. Replay rejected."
                )
            raise PermissionError(
                f"Plan '{plan_id}' is not in approved state (current: {stored_plan['status']}). Execution rejected."
            )

        if stored_plan.get("policy_outcome") not in (PolicyOutcome.AUTO_APPROVE.value, PolicyOutcome.CONFIRM.value):
            raise PermissionError("Plan has no executable policy outcome; clarify and blocked plans cannot execute.")

        actions = stored_plan["actions"]
        if not actions or compute_action_hash(actions) != stored_plan["action_hash"]:
            raise ValueError("Stored plan actions are empty or do not match their canonical action hash.")
        for act in actions:
            metadata = self.mcp.get_tool_metadata(act.get("operation", ""))
            if metadata is None or metadata.read_only:
                raise PermissionError(f"Operation '{act.get('operation')}' is not a registered state-changing tool.")
        if not stored_plan.get("intent"):
            raise PermissionError("Approved plan is missing its persisted intent and cannot be policy-validated.")
        policy_plan = CandidatePlan(
            plan_id=plan_id,
            intent=StructuredIntent.model_validate(stored_plan["intent"]),
            actor=stored_plan["actor"],
            user_role=stored_plan["user_role"],
            actions=[PlannedAction.model_validate(action) for action in actions],
            action_hash=stored_plan["action_hash"],
            policy_outcome=PolicyOutcome(stored_plan["policy_outcome"]),
            status=PlanStatus.APPROVED,
        )
        current_decision = PolicyEngine(self.mcp).evaluate(policy_plan)
        if current_decision.outcome in (PolicyOutcome.BLOCK, PolicyOutcome.CLARIFY) or (
            current_decision.outcome == PolicyOutcome.CONFIRM
            and stored_plan["policy_outcome"] != PolicyOutcome.CONFIRM.value
        ):
            raise PermissionError(f"Current policy does not authorize execution: {current_decision.reason}")

        # 3. Verify submitted hash matches stored hash
        if submitted_action_hash != stored_plan["action_hash"]:
            raise ValueError(
                f"ACTION_HASH_MISMATCH: Submitted hash '{submitted_action_hash}' does not match approved hash '{stored_plan['action_hash']}'."
            )

        # 4. Atomic compare-and-set to EXECUTING (Single-Use Replay Protection)
        transitioned = self.journal.transition_plan_to_executing(plan_id)
        if not transitioned:
            raise PermissionError(
                f"PLAN_ALREADY_CONSUMED: Failed atomic CAS transition. Plan '{plan_id}' was consumed concurrently."
            )

        executed_records = []
        observed_states: Dict[str, Dict[str, Any]] = {}

        try:
            for idx, act in enumerate(actions):
                metadata = self.mcp.get_tool_metadata(act["operation"])
                if metadata is None or metadata.read_only:
                    raise PermissionError(f"Operation '{act['operation']}' is not a registered state-changing tool.")
                # 5. Pre-execution Freshness Check (ADR-005)
                freshness = self.freshness_checker.check_freshness(
                    resource_type=act.get("resource_type", "calendar"),
                    resource_id=act["resource_id"],
                    expected_before_state=act.get("before_state", {}),
                )
                if not freshness.is_fresh:
                    self.journal.update_plan_status(plan_id, PlanStatus.FAILED)
                    compensation_result = None
                    if auto_compensate_on_failure and executed_records:
                        compensation_result = self.compensation_runner.compensate_plan(plan_id=plan_id)
                    suffix = f" | Saga compensation status: {compensation_result.status}" if compensation_result else ""
                    raise RuntimeError(f"{freshness.error_message}{suffix}")

                # 6. Write-Ahead Journal: Pre-Write before tool dispatch (ADR-006)
                rec_id = self.journal.log_pre_action(
                    plan_id=plan_id,
                    action_id=act["action_id"],
                    action_hash=stored_plan["action_hash"],
                    actor=stored_plan["actor"],
                    user_role=stored_plan["user_role"],
                    resource_type=act["resource_type"],
                    resource_id=act["resource_id"],
                    operation=act["operation"],
                    before_state=act.get("before_state", {}),
                    compensation_action=act.get("compensation_action"),
                )

                # Check if failure simulation requested for Beat 4
                should_fail = (
                    simulate_failure_at_action_index is not None
                    and idx == simulate_failure_at_action_index
                )

                # 7. Execute State-Changing Tool
                try:
                    result = self.mcp.dispatch_tool(
                        operation=act["operation"],
                        parameters=act.get("parameters", {}),
                        plan_id=plan_id,
                        action_hash=stored_plan["action_hash"],
                        simulate_failure=should_fail,
                    )
                    # 8. Write-Ahead Journal: Post-Write Success
                    if act["resource_type"] == "calendar":
                        live_after = self.mcp.get_calendar_event(act["resource_id"]) or result
                    elif act["resource_type"] == "tickets" and hasattr(self.mcp, "get_ticket"):
                        live_after = self.mcp.get_ticket(act["resource_id"]) or result
                    else:
                        live_after = result
                    self.journal.log_post_action_success(
                        record_id=rec_id,
                        after_state=live_after,
                        result=result,
                    )
                    executed_records.append(rec_id)
                    observed_states[act["resource_id"]] = live_after

                except Exception as exc:
                    # Record failure in write-ahead log
                    self.journal.log_post_action_failure(
                        record_id=rec_id, error_msg=str(exc)
                    )
                    self.journal.update_plan_status(plan_id, PlanStatus.FAILED)

                    # Trigger Saga Compensation for previously completed actions if requested
                    compensation_result = None
                    if auto_compensate_on_failure and executed_records:
                        compensation_result = self.compensation_runner.compensate_plan(
                            plan_id=plan_id
                        )

                    error_msg = f"Execution failed at action {idx} ('{act['action_id']}'): {exc}"
                    if compensation_result:
                        error_msg += f" | Saga compensation status: {compensation_result.status}"
                    raise RuntimeError(error_msg) from exc

                verification = InvariantChecker.verify_action(
                    PlannedAction.model_validate(act), live_after, metadata
                )
                if not verification.passed:
                    self.journal.update_plan_status(plan_id, PlanStatus.FAILED)
                    compensation_result = None
                    if auto_compensate_on_failure:
                        compensation_result = self.compensation_runner.compensate_plan(plan_id=plan_id)
                    raise RuntimeError(
                        f"Post-execution invariant verification failed: {verification.summary}"
                        + (f" | Saga compensation status: {compensation_result.status}" if compensation_result else "")
                    )

            # 9. All actions dispatched and verified successfully.
            if stored_plan.get("intent"):
                verification = InvariantChecker.verify_plan(
                    CandidatePlan(
                        plan_id=plan_id,
                        intent=StructuredIntent.model_validate(stored_plan["intent"]),
                        actor=stored_plan["actor"],
                        user_role=stored_plan["user_role"],
                        actions=[PlannedAction.model_validate(action) for action in actions],
                        action_hash=stored_plan["action_hash"],
                        policy_outcome=PolicyOutcome(stored_plan["policy_outcome"]),
                        status=PlanStatus.EXECUTING,
                    ),
                    observed_states,
                    {operation: self.mcp.get_tool_metadata(operation) for operation in {a["operation"] for a in actions}},
                )
                if not verification.passed:
                    self.journal.update_plan_status(plan_id, PlanStatus.FAILED)
                    compensation_result = self.compensation_runner.compensate_plan(plan_id) if auto_compensate_on_failure else None
                    raise RuntimeError(
                        f"Plan invariant verification failed: {verification.summary}"
                        + (f" | Saga compensation status: {compensation_result.status}" if compensation_result else "")
                    )
            self.journal.update_plan_status(plan_id, PlanStatus.DONE)
            return {
                "plan_id": plan_id,
                "status": PlanStatus.DONE.value,
                "executed_actions": len(actions),
                "journal_records": executed_records,
                "observed_states": observed_states,
            }

        except Exception:
            # Re-raise with plan status already handled
            raise
