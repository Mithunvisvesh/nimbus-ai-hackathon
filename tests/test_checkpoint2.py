"""Test suite for Phase 2 (Checkpoint 2 — Core Stability & Resilience).

Verifies Phase 2 Gate Criteria (DoD):
  1. Beat 1: Time conflict produces CLARIFY with suggested alternatives;
             re-planning with free alternative auto-approves and executes.
  2. Beat 2: Ambiguous afternoon scope with external client attendee produces CLARIFY.
  3. Multi-Action Failure & Saga Rollback: Step 2 failure triggers reverse compensation of Step 1.
  4. Pre-Compensation Drift Detection: External human edit prior to compensation halts
     rollback and logs a drift incident.
  5. Pre-Execution Freshness Checker: Detects stale resource state before tool dispatch.
"""

import tempfile
from pathlib import Path
import pytest

from src.schemas.intent import IntentConfidence, StructuredIntent, ConstraintType, IntentConstraint
from src.schemas.plan import CandidatePlan, PlannedAction, CompensationAction, PolicyOutcome, PlanStatus, RiskLevel
from src.schemas.journal import ActionJournalStatus
from src.intent.parser import IntentParser
from src.mcp.domains.calendar_module import CalendarDomainStore
from src.mcp.server import CareMCPServer
from src.planner.planner import DryRunPlanner
from src.policy.engine import PolicyEngine
from src.journal.db import ActionJournalDB
from src.executor.runner import ControlledExecutor
from src.integrity.freshness import FreshnessChecker
from src.recovery.drift_detector import DriftDetector
from src.recovery.compensation import SagaCompensationRunner


@pytest.fixture
def test_env():
    """Sets up an isolated MCP server and SQLite WAL database for testing."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "test_checkpoint2.db"
        calendar_store = CalendarDomainStore()
        calendar_store.reset()
        mcp_server = CareMCPServer(calendar_store=calendar_store)
        journal_db = ActionJournalDB(db_path=db_path)
        executor = ControlledExecutor(journal_db=journal_db, mcp_server=mcp_server)
        yield {
            "mcp_server": mcp_server,
            "calendar_store": calendar_store,
            "journal_db": journal_db,
            "executor": executor,
        }


def test_beat1_conflict_clarify_and_replan(test_env):
    """Beat 1: 'Move my 3 PM meeting to 5 PM' detects conflict at 5 PM -> CLARIFY -> user picks 4 PM -> AUTO-APPROVE."""
    mcp = test_env["mcp_server"]
    journal = test_env["journal_db"]
    executor = test_env["executor"]

    # Step 1: User asks for 5 PM
    parser = IntentParser(use_cache=False)
    intent = parser.parse("Move my 3 PM meeting to 5 PM")
    planner = DryRunPlanner(mcp_server=mcp)
    candidate_plan = planner.generate_candidate_plan(intent=intent)

    # 5 PM has conflict with 'Strategy Review' (evt_5pm_hold)
    policy_engine = PolicyEngine(mcp_server=mcp)
    eval_result = policy_engine.evaluate(candidate_plan)

    assert eval_result.outcome == PolicyOutcome.CLARIFY
    assert "Strategy Review" in eval_result.reason
    assert "2026-10-02T16:00:00Z" in eval_result.suggested_alternatives

    # Step 2: User selects suggested alternative (4 PM / 16:00)
    intent_replan = parser.parse("Move my 3 PM meeting to 4 PM")
    replan = planner.generate_candidate_plan(intent=intent_replan)
    eval_replan = policy_engine.evaluate(replan)

    assert eval_replan.outcome == PolicyOutcome.AUTO_APPROVE
    replan.policy_outcome = eval_replan.outcome
    replan.status = PlanStatus.APPROVED

    # Save and execute
    journal.save_approved_plan(replan)
    res = executor.execute_plan(replan.plan_id, replan.action_hash)

    assert res["status"] == PlanStatus.DONE.value
    updated_evt = mcp.get_calendar_event("evt_3pm_sync")
    assert "16:00" in updated_evt["start_time"]


def test_beat2_ambiguous_external_attendee_clarify(test_env):
    """Beat 2: 'Clear my afternoon' finds VIP client meeting -> CLARIFY due to external attendee ambiguity."""
    mcp = test_env["mcp_server"]
    parser = IntentParser(use_cache=False)
    intent = parser.parse("Clear my afternoon")

    assert intent.scope == "calendar"
    assert "unspecified_treatment_of_external_attendees" in intent.ambiguities

    planner = DryRunPlanner(mcp_server=mcp)
    plan = planner.generate_candidate_plan(intent=intent)

    # The plan contains afternoon events including evt_client_review with external VP
    policy_engine = PolicyEngine(mcp_server=mcp)
    eval_result = policy_engine.evaluate(plan)

    # Policy flags ambiguity
    assert eval_result.outcome == PolicyOutcome.CLARIFY
    assert "unspecified_treatment_of_external_attendees" in eval_result.reason


def test_multi_action_failure_and_reverse_saga_compensation(test_env):
    """Step 2 fails in a 2-step plan -> triggers automatic reverse saga compensation for Step 1."""
    mcp = test_env["mcp_server"]
    journal = test_env["journal_db"]
    executor = test_env["executor"]

    # Initial state
    evt1_before = mcp.get_calendar_event("evt_3pm_sync")
    evt2_before = mcp.get_calendar_event("evt_5pm_hold")
    assert "15:00" in evt1_before["start_time"]
    assert "17:00" in evt2_before["start_time"]

    # Create a 2-action plan
    plan_id = "plan_saga_test_01"
    actions = [
        PlannedAction(
            action_id="act_001",
            resource_type="calendar",
            resource_id="evt_3pm_sync",
            operation="calendar.update_event",
            parameters={"event_id": "evt_3pm_sync", "start_time": "2026-10-02T16:00:00Z"},
            before_state=dict(evt1_before),
            risk_level=RiskLevel.LOW,
            reversible=True,
            compensation_action=CompensationAction(
                operation="calendar.update_event",
                parameters={"event_id": "evt_3pm_sync", "start_time": evt1_before["start_time"]},
            ),
        ),
        PlannedAction(
            action_id="act_002",
            resource_type="calendar",
            resource_id="evt_5pm_hold",
            operation="calendar.update_event",
            parameters={"event_id": "evt_5pm_hold", "start_time": "2026-10-02T18:00:00Z"},
            before_state=dict(evt2_before),
            risk_level=RiskLevel.LOW,
            reversible=True,
            compensation_action=CompensationAction(
                operation="calendar.update_event",
                parameters={"event_id": "evt_5pm_hold", "start_time": evt2_before["start_time"]},
            ),
        ),
    ]

    from src.integrity.hasher import compute_action_hash
    action_hash = compute_action_hash(actions)

    plan = CandidatePlan(
        plan_id=plan_id,
        intent=StructuredIntent(goal="Batch shift", scope="calendar"),
        actor="user_mithun",
        user_role="STANDARD_USER",
        actions=actions,
        action_hash=action_hash,
        policy_outcome=PolicyOutcome.AUTO_APPROVE,
        status=PlanStatus.APPROVED,
    )
    journal.save_approved_plan(plan)

    # Execute with simulated failure on Step 2 (index 1)
    with pytest.raises(RuntimeError) as exc_info:
        executor.execute_plan(
            plan_id=plan_id,
            submitted_action_hash=action_hash,
            simulate_failure_at_action_index=1,
            auto_compensate_on_failure=True,
        )

    assert "Execution failed at action 1" in str(exc_info.value)
    assert "Saga compensation status: compensated" in str(exc_info.value)

    # Verify Step 1 was compensated back to its original before_state
    evt1_after = mcp.get_calendar_event("evt_3pm_sync")
    assert evt1_after["start_time"] == evt1_before["start_time"]

    # Verify journal statuses
    records = journal.get_journal_records(plan_id)
    assert len(records) == 2
    # Action 1 was executed and then compensated
    assert records[0].status == ActionJournalStatus.COMPENSATED
    # Action 2 failed
    assert records[1].status == ActionJournalStatus.FAILED

    # Verify plan status transitioned to COMPENSATED
    stored = journal.get_plan(plan_id)
    assert stored["status"] == PlanStatus.COMPENSATED.value


def test_drift_halts_compensation(test_env):
    """External drift occurs between execution and compensation -> halts rollback and logs incident."""
    mcp = test_env["mcp_server"]
    journal = test_env["journal_db"]

    plan_id = "plan_drift_test_01"
    evt = mcp.get_calendar_event("evt_3pm_sync")

    # Step 1: Pre-log and execute Action 1
    rec_id = journal.log_pre_action(
        plan_id=plan_id,
        action_id="act_drift_01",
        action_hash="hash123",
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

    # Tool executes -> updates to 16:00
    mcp.dispatch_tool(
        operation="calendar.update_event",
        parameters={"event_id": "evt_3pm_sync", "start_time": "2026-10-02T16:00:00Z"},
        plan_id=plan_id,
        action_hash="hash123",
    )
    journal.log_post_action_success(
        record_id=rec_id,
        after_state={"start_time": "2026-10-02T16:00:00Z"},
    )

    # EXTERNAL DRIFT: An external human reschedules to 16:30 directly behind agent's back
    mcp.calendar_store.events["evt_3pm_sync"]["start_time"] = "2026-10-02T16:30:00Z"

    # Now compensation runner attempts to compensate
    comp_runner = SagaCompensationRunner(journal_db=journal, mcp_server=mcp)
    comp_result = comp_runner.compensate_plan(plan_id=plan_id)

    assert comp_result.status == "drift"
    assert "halted due to external drift" in comp_result.message

    # The external human's modification (16:30) was NOT overwritten
    live_evt = mcp.get_calendar_event("evt_3pm_sync")
    assert live_evt["start_time"] == "2026-10-02T16:30:00Z"

    # Drift incident was logged
    incidents = journal.get_drift_incidents(plan_id=plan_id)
    assert len(incidents) == 1
    assert incidents[0]["resource_id"] == "evt_3pm_sync"
    assert "16:00" in str(incidents[0]["expected_after_state"])
    assert "16:30" in str(incidents[0]["observed_drift_state"])


def test_pre_execution_freshness_check(test_env):
    """Modifying resource state between planning and execution triggers STALE_RESOURCE_STATE abort."""
    mcp = test_env["mcp_server"]
    journal = test_env["journal_db"]
    executor = test_env["executor"]

    parser = IntentParser(use_cache=False)
    intent = parser.parse("Move 3 PM meeting to 4 PM")
    planner = DryRunPlanner(mcp_server=mcp)
    plan = planner.generate_candidate_plan(intent=intent)
    plan.policy_outcome = PolicyOutcome.AUTO_APPROVE
    plan.status = PlanStatus.APPROVED
    journal.save_approved_plan(plan)

    # External user changes the start time before agent executes
    mcp.calendar_store.events["evt_3pm_sync"]["start_time"] = "2026-10-02T15:45:00Z"

    with pytest.raises(RuntimeError) as exc_info:
        executor.execute_plan(plan.plan_id, plan.action_hash)

    assert "STALE_RESOURCE_STATE" in str(exc_info.value)
