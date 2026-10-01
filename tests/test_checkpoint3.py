"""Test suite for Phase 3 (Checkpoint 3 — Generalization, Drift & Demo Polish).

Verifies Phase 3 Gate Criteria (DoD):
  1. Beat 3: Role-Based Access Control (RBAC) & Consequential Ticket Gating:
     - READ_ONLY user attempting to mutate a ticket is deterministically BLOCKED.
     - STANDARD_USER attempting to close an escalated ticket (tkt_402) triggers CONFIRM.
     - STANDARD_USER attempting to close a standard ticket (tkt_105) AUTO-APPROVES and executes.
  2. Beat 4: Multi-Step Cross-Domain / Multi-Action Failure & Drift Protection:
     - Step 2 fails during execution.
     - External modification to Step 1's resource before compensation triggers DRIFT.
     - Blind compensation rollback is HALTED and human edits are preserved.
  3. Pre-Execution Freshness Check for Tickets domain:
     - Modifying ticket state prior to execution aborts with STALE_RESOURCE_STATE.
  4. Saga Compensation for Tickets domain:
     - Multi-ticket execution failure cleanly rolls back completed ticket to prior status.
"""

import tempfile
from pathlib import Path
import pytest

from src.schemas.intent import StructuredIntent
from src.schemas.plan import (
    CandidatePlan,
    PlannedAction,
    CompensationAction,
    PolicyOutcome,
    PlanStatus,
    RiskLevel,
)
from src.schemas.journal import ActionJournalStatus
from src.intent.parser import IntentParser
from src.mcp.domains.calendar_module import CalendarDomainStore
from src.mcp.domains.tickets_module import TicketsDomainStore
from src.mcp.server import CareMCPServer
from src.planner.planner import DryRunPlanner
from src.policy.engine import PolicyEngine
from src.journal.db import ActionJournalDB
from src.executor.runner import ControlledExecutor
from src.verifier.invariant_checker import InvariantChecker
from src.integrity.hasher import compute_action_hash


@pytest.fixture
def test_env():
    """Sets up an isolated MCP server with calendar + tickets and SQLite WAL database."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "test_checkpoint3.db"
        calendar_store = CalendarDomainStore()
        calendar_store.reset()
        tickets_store = TicketsDomainStore()
        tickets_store.reset()
        mcp_server = CareMCPServer(calendar_store=calendar_store, tickets_store=tickets_store)
        journal_db = ActionJournalDB(db_path=db_path)
        executor = ControlledExecutor(journal_db=journal_db, mcp_server=mcp_server)
        yield {
            "mcp_server": mcp_server,
            "calendar_store": calendar_store,
            "tickets_store": tickets_store,
            "journal_db": journal_db,
            "executor": executor,
        }


def test_beat3_rbac_readonly_blocked(test_env):
    """Beat 3 (Part A): READ_ONLY role attempting to mutate a ticket is BLOCKED."""
    mcp = test_env["mcp_server"]
    parser = IntentParser(use_cache=False)
    intent = parser.parse("Close escalated ticket #402")

    planner = DryRunPlanner(mcp_server=mcp)
    plan = planner.generate_candidate_plan(
        intent=intent,
        actor="user_intern",
        user_role="READ_ONLY",
    )

    policy_engine = PolicyEngine(mcp_server=mcp)
    eval_result = policy_engine.evaluate(plan)

    assert eval_result.outcome == PolicyOutcome.BLOCK
    assert "READ_ONLY" in eval_result.reason
    assert "tickets.update_status" in eval_result.reason


def test_beat3_escalated_ticket_requires_confirm_and_executes(test_env):
    """Beat 3 (Part B): STANDARD_USER attempting to close escalated ticket #402 requires CONFIRM.
    Upon explicit user confirmation, execution proceeds and succeeds."""
    mcp = test_env["mcp_server"]
    journal = test_env["journal_db"]
    executor = test_env["executor"]

    parser = IntentParser(use_cache=False)
    intent = parser.parse("Close escalated ticket #402")

    planner = DryRunPlanner(mcp_server=mcp)
    plan = planner.generate_candidate_plan(
        intent=intent,
        actor="user_mithun",
        user_role="STANDARD_USER",
    )

    policy_engine = PolicyEngine(mcp_server=mcp)
    eval_result = policy_engine.evaluate(plan)

    # Escalated ticket requires explicit user CONFIRM
    assert eval_result.outcome == PolicyOutcome.CONFIRM
    assert "active escalation" in eval_result.reason or "escalation" in eval_result.reason

    # Simulated User Confirmation Flow
    plan.policy_outcome = eval_result.outcome
    plan.status = PlanStatus.APPROVED
    journal.save_approved_plan(plan, explicit_confirmation=True)

    # Execute approved plan
    exec_res = executor.execute_plan(plan.plan_id, plan.action_hash)
    assert exec_res["status"] == PlanStatus.DONE.value

    # Verify ticket state in live store
    tkt_after = mcp.get_ticket("tkt_402")
    assert tkt_after["status"] == "closed"

    # Post-execution Invariant Verification
    post_states = {"tkt_402": tkt_after}
    verify_res = InvariantChecker.verify_plan(plan, post_states)
    assert verify_res.passed is True


def test_beat3_standard_ticket_auto_approves_and_executes(test_env):
    """Beat 3 (Part C): STANDARD_USER closing standard non-escalated ticket #105 AUTO-APPROVES."""
    mcp = test_env["mcp_server"]
    journal = test_env["journal_db"]
    executor = test_env["executor"]

    parser = IntentParser(use_cache=False)
    intent = parser.parse("Close ticket #105")

    planner = DryRunPlanner(mcp_server=mcp)
    plan = planner.generate_candidate_plan(
        intent=intent,
        actor="user_mithun",
        user_role="STANDARD_USER",
    )

    policy_engine = PolicyEngine(mcp_server=mcp)
    eval_result = policy_engine.evaluate(plan)

    assert eval_result.outcome == PolicyOutcome.AUTO_APPROVE
    assert "All actions are authorized" in eval_result.reason

    plan.policy_outcome = eval_result.outcome
    plan.status = PlanStatus.APPROVED
    journal.save_approved_plan(plan)

    exec_res = executor.execute_plan(plan.plan_id, plan.action_hash)
    assert exec_res["status"] == PlanStatus.DONE.value

    tkt_after = mcp.get_ticket("tkt_105")
    assert tkt_after["status"] == "closed"


def test_beat4_multi_domain_failure_and_drift_halts_rollback(test_env):
    """Beat 4: Multi-step cross-domain plan (Calendar + Ticket) fails at Step 2.
    Before Step 1 compensation runs, an external human reschedules Step 1's event.
    The system detects DRIFT and halts automatic rollback, protecting the human's edit."""
    mcp = test_env["mcp_server"]
    journal = test_env["journal_db"]
    executor = test_env["executor"]

    evt_init = mcp.get_calendar_event("evt_3pm_sync")
    tkt_init = mcp.get_ticket("tkt_105")

    plan_id = "plan_cross_domain_beat4"
    actions = [
        PlannedAction(
            action_id="act_step1_cal",
            resource_type="calendar",
            resource_id="evt_3pm_sync",
            operation="calendar.update_event",
            parameters={"event_id": "evt_3pm_sync", "start_time": "2026-10-02T16:00:00Z"},
            before_state=dict(evt_init),
            risk_level=RiskLevel.LOW,
            reversible=True,
            compensation_action=CompensationAction(
                operation="calendar.update_event",
                parameters={"event_id": "evt_3pm_sync", "start_time": evt_init["start_time"]},
            ),
        ),
        PlannedAction(
            action_id="act_step2_tkt",
            resource_type="tickets",
            resource_id="tkt_105",
            operation="tickets.update_status",
            parameters={"ticket_id": "tkt_105", "new_status": "closed"},
            before_state=dict(tkt_init),
            risk_level=RiskLevel.LOW,
            reversible=True,
            compensation_action=CompensationAction(
                operation="tickets.update_status",
                parameters={"ticket_id": "tkt_105", "new_status": tkt_init["status"]},
            ),
        ),
    ]

    action_hash = compute_action_hash(actions)
    plan = CandidatePlan(
        plan_id=plan_id,
        intent=StructuredIntent(goal="Afternoon wrap-up", scope="general"),
        actor="user_mithun",
        user_role="STANDARD_USER",
        actions=actions,
        action_hash=action_hash,
        policy_outcome=PolicyOutcome.CONFIRM,
        status=PlanStatus.APPROVED,
    )
    journal.save_approved_plan(plan, explicit_confirmation=True)

    # 1. Step 1 executes successfully through ControlledExecutor
    # 2. Before Step 2, an external human edits the calendar event to 16:45 directly
    # 3. Step 2 fails with simulated failure
    # To test this exact sequence, we can inject a hook or execute step 1, modify live state, and run with failure:

    # Execute step 1 manually or simulate failure during runner execution:
    # First, let's execute Action 1 via normal executor:
    rec_id1 = journal.log_pre_action(
        plan_id=plan_id,
        action_id=actions[0].action_id,
        action_hash=action_hash,
        actor=plan.actor,
        user_role=plan.user_role,
        resource_type=actions[0].resource_type,
        resource_id=actions[0].resource_id,
        operation=actions[0].operation,
        before_state=actions[0].before_state,
        compensation_action=actions[0].compensation_action.model_dump(),
    )
    res1 = mcp.dispatch_tool(
        operation=actions[0].operation,
        parameters=actions[0].parameters,
        plan_id=plan_id,
        action_hash=action_hash,
    )
    journal.log_post_action_success(
        record_id=rec_id1,
        after_state={"start_time": "2026-10-02T16:00:00Z"},
        result=res1,
    )

    # SIMULATE EXTERNAL HUMAN DRIFT: Human reschedules to 16:45
    mcp.calendar_store.events["evt_3pm_sync"]["start_time"] = "2026-10-02T16:45:00Z"

    # Step 2 begins and fails
    rec_id2 = journal.log_pre_action(
        plan_id=plan_id,
        action_id=actions[1].action_id,
        action_hash=action_hash,
        actor=plan.actor,
        user_role=plan.user_role,
        resource_type=actions[1].resource_type,
        resource_id=actions[1].resource_id,
        operation=actions[1].operation,
        before_state=actions[1].before_state,
        compensation_action=actions[1].compensation_action.model_dump(),
    )
    journal.log_post_action_failure(rec_id2, "Simulated network timeout on ticket gateway")
    journal.update_plan_status(plan_id, PlanStatus.FAILED)

    # Trigger Saga Compensation Runner
    from src.recovery.compensation import SagaCompensationRunner
    comp_runner = SagaCompensationRunner(journal_db=journal, mcp_server=mcp)
    comp_res = comp_runner.compensate_plan(plan_id=plan_id)

    # Assert that compensation halted due to drift
    assert comp_res.status == "drift"
    assert "halted due to external drift" in comp_res.message

    # Assert human edit was NOT overwritten
    live_evt = mcp.get_calendar_event("evt_3pm_sync")
    assert live_evt["start_time"] == "2026-10-02T16:45:00Z"

    # Assert drift incident logged
    incidents = journal.get_drift_incidents(plan_id=plan_id)
    assert len(incidents) == 1
    assert incidents[0]["resource_id"] == "evt_3pm_sync"
    assert "16:45" in str(incidents[0]["observed_drift_state"])


def test_tickets_pre_execution_freshness_check(test_env):
    """Pre-execution freshness check detects external modification to a ticket."""
    mcp = test_env["mcp_server"]
    journal = test_env["journal_db"]
    executor = test_env["executor"]

    parser = IntentParser(use_cache=False)
    intent = parser.parse("Close ticket #105")

    planner = DryRunPlanner(mcp_server=mcp)
    plan = planner.generate_candidate_plan(intent=intent)
    plan.policy_outcome = PolicyOutcome.AUTO_APPROVE
    plan.status = PlanStatus.APPROVED
    journal.save_approved_plan(plan)

    # External modification between planning and execution
    mcp.tickets_store.tickets["tkt_105"]["status"] = "in_progress"

    with pytest.raises(RuntimeError) as exc_info:
        executor.execute_plan(plan.plan_id, plan.action_hash)

    assert "STALE_RESOURCE_STATE" in str(exc_info.value)
    assert "tkt_105" in str(exc_info.value)


def test_tickets_saga_compensation_success(test_env):
    """Multi-ticket plan with Step 2 failure cleanly compensates Step 1."""
    mcp = test_env["mcp_server"]
    journal = test_env["journal_db"]
    executor = test_env["executor"]

    tkt105_before = mcp.get_ticket("tkt_105")
    tkt402_before = mcp.get_ticket("tkt_402")

    plan_id = "plan_multi_tickets_comp"
    actions = [
        PlannedAction(
            action_id="act_tkt1",
            resource_type="tickets",
            resource_id="tkt_105",
            operation="tickets.update_status",
            parameters={"ticket_id": "tkt_105", "new_status": "in_progress"},
            before_state=dict(tkt105_before),
            risk_level=RiskLevel.LOW,
            reversible=True,
            compensation_action=CompensationAction(
                operation="tickets.update_status",
                parameters={"ticket_id": "tkt_105", "new_status": tkt105_before["status"]},
            ),
        ),
        PlannedAction(
            action_id="act_tkt2",
            resource_type="tickets",
            resource_id="tkt_402",
            operation="tickets.update_status",
            parameters={"ticket_id": "tkt_402", "new_status": "resolved"},
            before_state=dict(tkt402_before),
            risk_level=RiskLevel.LOW,
            reversible=True,
            compensation_action=CompensationAction(
                operation="tickets.update_status",
                parameters={"ticket_id": "tkt_402", "new_status": tkt402_before["status"]},
            ),
        ),
    ]

    action_hash = compute_action_hash(actions)
    plan = CandidatePlan(
        plan_id=plan_id,
        intent=StructuredIntent(goal="Batch ticket triage", scope="tickets"),
        actor="user_mithun",
        user_role="ADMIN",
        actions=actions,
        action_hash=action_hash,
        policy_outcome=PolicyOutcome.CONFIRM,
        status=PlanStatus.APPROVED,
    )
    journal.save_approved_plan(plan, explicit_confirmation=True)

    # Execute with simulated failure on Step 2
    with pytest.raises(RuntimeError) as exc_info:
        executor.execute_plan(
            plan_id=plan_id,
            submitted_action_hash=action_hash,
            simulate_failure_at_action_index=1,
            auto_compensate_on_failure=True,
        )

    assert "Execution failed at action 1" in str(exc_info.value)
    assert "Saga compensation status: compensated" in str(exc_info.value)

    # Step 1 was compensated back to 'open'
    assert mcp.get_ticket("tkt_105")["status"] == tkt105_before["status"]

    records = journal.get_journal_records(plan_id)
    assert len(records) == 2
    assert records[0].status == ActionJournalStatus.COMPENSATED
    assert records[1].status == ActionJournalStatus.FAILED
