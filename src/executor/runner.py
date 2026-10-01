from typing import Any, Dict, List, Optional
from src.schemas.plan import PlanStatus
from src.schemas.journal import ActionJournalStatus
from src.journal.db import ActionJournalDB
from src.mcp.server import CareMCPServer

class ControlledExecutor:
    """
    Deterministic Controlled Executor.
    Enforces plan hash verification, single-use replay protection,
    pre-execution resource freshness checking, and write-ahead journaling.
    """
    def __init__(self, journal_db: ActionJournalDB, mcp_server: CareMCPServer):
        self.journal = journal_db
        self.mcp = mcp_server

    def execute_plan(
        self,
        plan_id: str,
        submitted_action_hash: str,
        simulate_failure_at_action_index: Optional[int] = None,
    ) -> Dict[str, Any]:
        # 1. Fetch stored plan from durable store
        stored_plan = self.journal.get_plan(plan_id)
        if not stored_plan:
            raise KeyError(f"Plan '{plan_id}' does not exist in durable store.")

        # 2. Verify stored status is APPROVED
        if stored_plan["status"] != PlanStatus.APPROVED.value:
            if stored_plan["status"] in (PlanStatus.EXECUTING.value, PlanStatus.DONE.value):
                raise PermissionError(
                    f"PLAN_ALREADY_CONSUMED: Plan '{plan_id}' is already {stored_plan['status']}. Replay rejected."
                )
            raise PermissionError(
                f"Plan '{plan_id}' is not in approved state (current: {stored_plan['status']}). Execution rejected."
            )

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

        actions = stored_plan["actions"]
        executed_records = []

        try:
            for idx, act in enumerate(actions):
                # 5. Pre-execution Freshness Check
                if act.get("resource_type") == "calendar":
                    live_event = self.mcp.get_calendar_event(act["resource_id"])
                    if not live_event:
                        raise RuntimeError(f"Resource '{act['resource_id']}' no longer exists.")
                    
                    # Verify key before_state attributes match live resource
                    expected_before = act.get("before_state", {})
                    for key in ["start_time", "end_time", "title"]:
                        if key in expected_before and expected_before[key] != live_event.get(key):
                            self.journal.update_plan_status(plan_id, PlanStatus.FAILED)
                            raise RuntimeError(
                                f"STALE_RESOURCE_STATE: Resource '{act['resource_id']}' has changed. "
                                f"Expected {key}={expected_before[key]}, live={live_event.get(key)}."
                            )

                # 6. Write-Ahead Journal (Pre-Write)
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

                # Check if failure simulation requested
                should_fail = (simulate_failure_at_action_index is not None and idx == simulate_failure_at_action_index)

                # 7. Execute State-Changing Tool
                try:
                    result = self.mcp.dispatch_tool(
                        operation=act["operation"],
                        parameters=act.get("parameters", {}),
                        plan_id=plan_id,
                        action_hash=stored_plan["action_hash"],
                        simulate_failure=should_fail,
                    )
                    # 8. Write-Ahead Journal (Post-Write Success)
                    after_state = self.mcp.get_calendar_event(act["resource_id"]) or result
                    self.journal.log_post_action_success(
                        record_id=rec_id,
                        after_state=after_state,
                        result=result,
                    )
                    executed_records.append(rec_id)
                except Exception as exc:
                    self.journal.log_post_action_failure(record_id=rec_id, error_msg=str(exc))
                    self.journal.update_plan_status(plan_id, PlanStatus.FAILED)
                    raise

            # 9. All actions succeeded
            self.journal.update_plan_status(plan_id, PlanStatus.DONE)
            return {
                "plan_id": plan_id,
                "status": PlanStatus.DONE.value,
                "executed_actions": len(actions),
                "journal_records": executed_records,
            }

        except Exception as e:
            # Plan marked failed
            self.journal.update_plan_status(plan_id, PlanStatus.FAILED)
            raise
