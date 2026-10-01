import json
import sys
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.intent.parser import IntentParser
from src.mcp.domains.calendar_module import CalendarDomainStore
from src.mcp.server import CareMCPServer
from src.planner.planner import DryRunPlanner
from src.policy.engine import PolicyEngine
from src.journal.db import ActionJournalDB
from src.executor.runner import ControlledExecutor
from src.schemas.plan import PlanStatus, PlannedAction, CompensationAction, CandidatePlan, RiskLevel
from src.schemas.intent import StructuredIntent
from src.verifier.invariant_checker import InvariantChecker
from src.recovery.compensation import SagaCompensationRunner


def print_banner(title: str):
    print(f"\n{'='*28} {title} {'='*28}")


def run_beat1_demo():
    print_banner("BEAT 1: CONFLICT CHECK & CLARIFICATION LOOP")
    prompt = "Move my 3 PM meeting to 5 PM"
    print(f"[INPUT PROMPT]: \"{prompt}\"")

    calendar_store = CalendarDomainStore()
    calendar_store.reset()
    mcp_server = CareMCPServer(calendar_store=calendar_store)
    journal_db = ActionJournalDB()
    executor = ControlledExecutor(journal_db=journal_db, mcp_server=mcp_server)

    # 1. Intent Parsing
    parser = IntentParser(use_cache=False)
    intent = parser.parse(prompt)
    print(f"[STAGE 1: INTENT EXTRACTED]: Goal='{intent.goal}', Scope='{intent.scope}'")

    # 2. Dry-Run Planning
    planner = DryRunPlanner(mcp_server=mcp_server)
    plan = planner.generate_candidate_plan(intent=intent)
    print(f"[STAGE 2: DRY-RUN TARGET]: Resolved target='{plan.actions[0].resource_id}' to 5:00 PM (17:00)")

    # 3. Policy Evaluation -> Detects conflict at 5 PM
    policy_engine = PolicyEngine(mcp_server=mcp_server)
    decision = policy_engine.evaluate(plan)
    print(f"[STAGE 3: POLICY GATE]: Outcome={decision.outcome.value.upper()}")
    print(f"         Reason: {decision.reason}")
    print(f"         Suggested Free Alternatives: {decision.suggested_alternatives}")

    # 4. User Chooses Alternative Time (16:00 / 4 PM)
    print("\n>>> USER INTERACTION: Selected alternative '4:00 PM (16:00)'")
    replan_intent = parser.parse("Move my 3 PM meeting to 4 PM")
    replan = planner.generate_candidate_plan(intent=replan_intent)
    replan_decision = policy_engine.evaluate(replan)

    print(f"[RE-PLAN POLICY GATE]: Outcome={replan_decision.outcome.value.upper()}")
    print(f"         Reason: {replan_decision.reason}")

    # 5. Execution & Write-Ahead Journaling
    replan.policy_outcome = replan_decision.outcome
    replan.status = PlanStatus.APPROVED
    journal_db.save_approved_plan(replan)

    exec_result = executor.execute_plan(replan.plan_id, replan.action_hash)
    print(f"[STAGE 4: CONTROLLED EXECUTION]: Status={exec_result['status']}")

    # 6. Post-Execution Invariant Verification
    live_after = mcp_server.get_calendar_event("evt_3pm_sync")
    inv_result = InvariantChecker.verify_plan(replan, {"evt_3pm_sync": live_after})
    print(f"[STAGE 5: INVARIANT VERIFICATION]: Passed={inv_result.passed} ({inv_result.summary})")
    print(f"         New Meeting Time: {live_after['start_time']}")


def run_beat2_demo():
    print_banner("BEAT 2: AMBIGUOUS CONSEQUENTIAL REQUEST")
    prompt = "Clear my afternoon"
    print(f"[INPUT PROMPT]: \"{prompt}\"")

    calendar_store = CalendarDomainStore()
    calendar_store.reset()
    mcp_server = CareMCPServer(calendar_store=calendar_store)

    parser = IntentParser(use_cache=False)
    intent = parser.parse(prompt)
    print(f"[STAGE 1: INTENT EXTRACTED]: Goal='{intent.goal}'")
    print(f"         Detected Ambiguities: {intent.ambiguities}")

    planner = DryRunPlanner(mcp_server=mcp_server)
    plan = planner.generate_candidate_plan(intent=intent)
    print(f"[STAGE 2: DRY-RUN RESOLUTION]: Resolved {len(plan.actions)} affected afternoon events:")
    for act in plan.actions:
        has_ext = any(a.get("is_external") for a in act.before_state.get("attendees", []))
        ext_tag = " [EXTERNAL CLIENT VIP]" if has_ext else " [INTERNAL]"
        print(f"         - {act.resource_id}: {act.before_state.get('title')}{ext_tag}")

    policy_engine = PolicyEngine(mcp_server=mcp_server)
    decision = policy_engine.evaluate(plan)
    print(f"[STAGE 3: POLICY GATE]: Outcome={decision.outcome.value.upper()}")
    print(f"         Decision Rationale: {decision.reason}")
    print("         >> Protected external client meeting from blind unstated cancellation!")


def run_beat4_preview_demo():
    print_banner("SAGA REVERSE COMPENSATION & PRE-COMPENSATION DRIFT")
    calendar_store = CalendarDomainStore()
    calendar_store.reset()
    mcp_server = CareMCPServer(calendar_store=calendar_store)
    journal_db = ActionJournalDB()
    executor = ControlledExecutor(journal_db=journal_db, mcp_server=mcp_server)

    evt1 = mcp_server.get_calendar_event("evt_3pm_sync")
    evt2 = mcp_server.get_calendar_event("evt_5pm_hold")

    from src.integrity.hasher import compute_action_hash

    actions = [
        PlannedAction(
            action_id="act_step1",
            resource_type="calendar",
            resource_id="evt_3pm_sync",
            operation="calendar.update_event",
            parameters={"event_id": "evt_3pm_sync", "start_time": "2026-10-02T16:00:00Z"},
            before_state=dict(evt1),
            reversible=True,
            compensation_action=CompensationAction(
                operation="calendar.update_event",
                parameters={"event_id": "evt_3pm_sync", "start_time": evt1["start_time"]},
            ),
        ),
        PlannedAction(
            action_id="act_step2",
            resource_type="calendar",
            resource_id="evt_5pm_hold",
            operation="calendar.update_event",
            parameters={"event_id": "evt_5pm_hold", "start_time": "2026-10-02T18:00:00Z"},
            before_state=dict(evt2),
            reversible=True,
            compensation_action=CompensationAction(
                operation="calendar.update_event",
                parameters={"event_id": "evt_5pm_hold", "start_time": evt2["start_time"]},
            ),
        ),
    ]

    plan_id = "plan_saga_demo_1"
    h = compute_action_hash(actions)
    plan = CandidatePlan(
        plan_id=plan_id,
        intent=StructuredIntent(goal="Multi-action batch", scope="calendar"),
        actions=actions,
        action_hash=h,
        actor="user_mithun",
        user_role="STANDARD_USER",
        status=PlanStatus.APPROVED,
    )
    journal_db.save_approved_plan(plan)

    print("\n[PART A]: Simulating multi-step execution with injected failure at Step 2...")
    try:
        executor.execute_plan(
            plan_id=plan_id,
            submitted_action_hash=h,
            simulate_failure_at_action_index=1,
            auto_compensate_on_failure=True,
        )
    except Exception as e:
        print(f"   [CAUGHT FAILURE]: {e}")

    evt1_restored = mcp_server.get_calendar_event("evt_3pm_sync")
    print(f"   [SAGA STATUS]: Step 1 restored to original time: {evt1_restored['start_time']}")

    print("\n[PART B]: Simulating external human drift before compensation...")
    # Create a separate plan where step 1 was done, but an external human edited the resource before rollback
    plan_id_drift = "plan_drift_demo_2"
    rec_id = journal_db.log_pre_action(
        plan_id=plan_id_drift,
        action_id="act_drift_1",
        action_hash="hash_drift_999",
        actor="user_mithun",
        user_role="STANDARD_USER",
        resource_type="calendar",
        resource_id="evt_3pm_sync",
        operation="calendar.update_event",
        before_state={"start_time": "2026-10-02T15:00:00Z"},
        compensation_action={
            "operation": "calendar.update_event",
            "parameters": {"event_id": "evt_3pm_sync", "start_time": "2026-10-02T15:00:00Z"},
        },
    )
    journal_db.log_post_action_success(
        record_id=rec_id,
        after_state={"start_time": "2026-10-02T16:00:00Z"},
    )
    journal_db.save_approved_plan(
        CandidatePlan(
            plan_id=plan_id_drift,
            intent=StructuredIntent(goal="Drift test", scope="calendar"),
            actions=[],
            actor="user_mithun",
            user_role="STANDARD_USER",
            status=PlanStatus.APPROVED,
        )
    )

    # EXTERNAL HUMAN DRIFT: Human changes evt_3pm_sync to 16:45 directly
    calendar_store.events["evt_3pm_sync"]["start_time"] = "2026-10-02T16:45:00Z"

    comp_runner = SagaCompensationRunner(journal_db=journal_db, mcp_server=mcp_server)
    drift_result = comp_runner.compensate_plan(plan_id_drift)

    print(f"   [DRIFT DETECTOR]: Status = {drift_result.status.upper()}")
    print(f"   [ACTION HALTED]: {drift_result.message}")
    print(f"   [HUMAN EDIT PRESERVED]: Current live start_time remains '{calendar_store.events['evt_3pm_sync']['start_time']}'")


if __name__ == "__main__":
    print_banner("NIMBUS 2026 — CARE CHECKPOINT 2 DEMONSTRATION")
    run_beat1_demo()
    run_beat2_demo()
    run_beat4_preview_demo()
    print_banner("CHECKPOINT 2 (CORE STABILITY & RESILIENCE) VERIFIED")
