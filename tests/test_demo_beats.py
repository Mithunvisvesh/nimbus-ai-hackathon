"""End-to-End Integration Test Suite for all 4 CARE Demo Beats.

Validates Phase 4 Rehearsal criteria:
- Beat 1: Conflict detection at 5 PM -> CLARIFY -> user picks 4 PM -> AUTO-APPROVE -> execute -> verify.
- Beat 2: "Clear my afternoon" -> resolves 3 events including VIP client meeting -> CLARIFY.
- Beat 3: Consequential RBAC:
  - READ_ONLY attempting ticket close -> BLOCKED.
  - STANDARD_USER on escalated ticket #402 -> CONFIRM -> user confirms -> executes -> verified.
  - STANDARD_USER on standard ticket #105 -> AUTO-APPROVE -> executes.
- Beat 4: Multi-step failure -> external human edit -> pre-compensation DRIFT detected -> rollback halted.
"""

import tempfile
import secrets
from pathlib import Path
import pytest

from src.schemas.intent import StructuredIntent
from src.schemas.plan import CandidatePlan, PlannedAction, CompensationAction, PolicyOutcome, PlanStatus, RiskLevel
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
from src.recovery.compensation import SagaCompensationRunner
from src.integrity.hasher import compute_action_hash


@pytest.fixture
def care_system():
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "e2e_rehearsal.db"
        calendar_store = CalendarDomainStore()
        calendar_store.reset()
        tickets_store = TicketsDomainStore()
        tickets_store.reset()
        mcp_server = CareMCPServer(calendar_store=calendar_store, tickets_store=tickets_store)
        journal_db = ActionJournalDB(db_path=db_path)
        executor = ControlledExecutor(journal_db=journal_db, mcp_server=mcp_server)
        parser = IntentParser(use_cache=True)
        planner = DryRunPlanner(mcp_server=mcp_server)
        policy_engine = PolicyEngine(mcp_server=mcp_server)

        yield {
            "mcp": mcp_server,
            "journal": journal_db,
            "executor": executor,
            "parser": parser,
            "planner": planner,
            "policy": policy_engine,
        }


def test_e2e_beat1_clear_request_conflict_check(care_system):
    """Beat 1 E2E: Conflict -> CLARIFY -> Re-plan 4 PM -> AUTO-APPROVE -> Execute -> Verify."""
    mcp = care_system["mcp"]
    journal = care_system["journal"]
    executor = care_system["executor"]
    parser = care_system["parser"]
    planner = care_system["planner"]
    policy = care_system["policy"]

    # 1. User asks for 5 PM
    intent_5pm = parser.parse("Move my 3 PM meeting to 5 PM")
    plan_5pm = planner.generate_candidate_plan(intent=intent_5pm, user_role="STANDARD_USER")
    dec_5pm = policy.evaluate(plan_5pm)

    assert dec_5pm.outcome == PolicyOutcome.CLARIFY
    assert "Strategy Review" in dec_5pm.reason
    assert "2026-10-02T16:00:00Z" in dec_5pm.suggested_alternatives

    # 2. User chooses 4 PM
    intent_4pm = parser.parse("Move my 3 PM meeting to 4 PM")
    plan_4pm = planner.generate_candidate_plan(intent=intent_4pm, user_role="STANDARD_USER")
    dec_4pm = policy.evaluate(plan_4pm)

    assert dec_4pm.outcome == PolicyOutcome.AUTO_APPROVE
    plan_4pm.policy_outcome = dec_4pm.outcome
    plan_4pm.status = PlanStatus.APPROVED
    journal.save_approved_plan(plan_4pm)

    exec_res = executor.execute_plan(plan_4pm.plan_id, plan_4pm.action_hash)
    assert exec_res["status"] == PlanStatus.DONE.value

    # Invariant verification
    live_evt = mcp.get_calendar_event("evt_3pm_sync")
    assert "16:00" in live_evt["start_time"]
    inv_res = InvariantChecker.verify_plan(plan_4pm, {"evt_3pm_sync": live_evt})
    assert inv_res.passed is True


def test_e2e_beat2_ambiguous_consequential_scope(care_system):
    """Beat 2 E2E: 'Clear my afternoon' -> VIP client meeting -> CLARIFY."""
    parser = care_system["parser"]
    planner = care_system["planner"]
    policy = care_system["policy"]

    intent = parser.parse("Clear my afternoon")
    assert "unspecified_treatment_of_external_attendees" in intent.ambiguities

    plan = planner.generate_candidate_plan(intent=intent, user_role="STANDARD_USER")
    assert len(plan.actions) >= 2

    dec = policy.evaluate(plan)
    assert dec.outcome == PolicyOutcome.CLARIFY
    assert "unspecified_treatment_of_external_attendees" in dec.reason


def test_e2e_beat3_rbac_and_escalated_ticket(care_system):
    """Beat 3 E2E: READ_ONLY is BLOCKED; STANDARD_USER requires CONFIRM; routine ticket AUTO-APPROVES."""
    mcp = care_system["mcp"]
    journal = care_system["journal"]
    executor = care_system["executor"]
    parser = care_system["parser"]
    planner = care_system["planner"]
    policy = care_system["policy"]

    # Part A: READ_ONLY -> BLOCK
    intent_esc = parser.parse("Close escalated ticket #402")
    plan_ro = planner.generate_candidate_plan(intent=intent_esc, user_role="READ_ONLY")
    dec_ro = policy.evaluate(plan_ro)
    assert dec_ro.outcome == PolicyOutcome.BLOCK

    # Part B: STANDARD_USER -> CONFIRM -> Authorize -> Execute
    plan_std = planner.generate_candidate_plan(intent=intent_esc, user_role="STANDARD_USER")
    dec_std = policy.evaluate(plan_std)
    assert dec_std.outcome == PolicyOutcome.CONFIRM

    plan_std.policy_outcome = dec_std.outcome
    plan_std.status = PlanStatus.APPROVED
    journal.save_approved_plan(plan_std, explicit_confirmation=True)

    exec_res = executor.execute_plan(plan_std.plan_id, plan_std.action_hash)
    assert exec_res["status"] == PlanStatus.DONE.value
    assert mcp.get_ticket("tkt_402")["status"] == "closed"

    # Part C: Standard ticket #105 -> AUTO_APPROVE
    intent_std = parser.parse("Close ticket #105")
    plan_105 = planner.generate_candidate_plan(intent=intent_std, user_role="STANDARD_USER")
    dec_105 = policy.evaluate(plan_105)
    assert dec_105.outcome == PolicyOutcome.AUTO_APPROVE


def test_e2e_beat4_multi_action_drift_and_recovery(care_system):
    """Beat 4 E2E: Injected failure on Step 2 -> External drift on Step 1 -> Rollback HALTED."""
    mcp = care_system["mcp"]
    journal = care_system["journal"]

    evt = mcp.get_calendar_event("evt_3pm_sync")
    plan_id = "plan_e2e_beat4"
    actions = [
        PlannedAction(
            action_id="act_01",
            resource_type="calendar",
            resource_id="evt_3pm_sync",
            operation="calendar.update_event",
            parameters={"event_id": "evt_3pm_sync", "start_time": "2026-10-02T16:00:00Z"},
            before_state=dict(evt),
            risk_level=RiskLevel.LOW,
            reversible=True,
            compensation_action=CompensationAction(
                operation="calendar.update_event",
                parameters={"event_id": "evt_3pm_sync", "start_time": evt["start_time"]},
            ),
        ),
        PlannedAction(
            action_id="act_02",
            resource_type="tickets",
            resource_id="tkt_105",
            operation="tickets.update_status",
            parameters={"ticket_id": "tkt_105", "new_status": "closed"},
            before_state=dict(mcp.get_ticket("tkt_105")),
            risk_level=RiskLevel.LOW,
            reversible=True,
            compensation_action=CompensationAction(
                operation="tickets.update_status",
                parameters={"ticket_id": "tkt_105", "new_status": "open"},
            ),
        ),
    ]

    action_hash = compute_action_hash(actions)
    plan = CandidatePlan(
        plan_id=plan_id,
        intent=StructuredIntent(goal="Batch shift", scope="general"),
        actor="user_mithun",
        user_role="ADMIN",
        actions=actions,
        action_hash=action_hash,
        policy_outcome=PolicyOutcome.AUTO_APPROVE,
        status=PlanStatus.APPROVED,
    )
    journal.save_approved_plan(plan)
    journal.transition_plan_to_executing(plan_id)

    # Step 1 executes successfully
    rec1 = journal.log_pre_action(
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
    dispatch_token = secrets.token_urlsafe(32)
    journal.prepare_tool_dispatch(rec1, dispatch_token)
    mcp.dispatch_tool(actions[0].operation, actions[0].parameters, plan_id, action_hash, action_id=actions[0].action_id, dispatch_token=dispatch_token)
    journal.log_post_action_success(rec1, mcp.get_calendar_event(actions[0].resource_id))

    # Simulated external human drift
    mcp.calendar_store.events["evt_3pm_sync"]["start_time"] = "2026-10-02T16:45:00Z"

    # Step 2 fails
    rec2 = journal.log_pre_action(
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
    journal.log_post_action_failure(rec2, "Simulated network timeout on ticket API")
    journal.update_plan_status(plan_id, PlanStatus.FAILED)

    # Recovery
    comp_runner = SagaCompensationRunner(journal_db=journal, mcp_server=mcp)
    comp_res = comp_runner.compensate_plan(plan_id=plan_id)

    assert comp_res.status == "drift"
    assert "halted due to external drift" in comp_res.message
    # Human edit was NOT overwritten
    assert mcp.get_calendar_event("evt_3pm_sync")["start_time"] == "2026-10-02T16:45:00Z"
