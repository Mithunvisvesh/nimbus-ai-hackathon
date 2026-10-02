"""End-to-End Scenarios Validation for CARE Framework.
Validates Scenarios A, B, C, D, and E as required by project specification.

Usage:
    python demo/run_end_to_end_scenarios.py
"""

import json
import secrets
import sys
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.intent.parser import IntentParser
from src.mcp.domains.calendar_module import CalendarDomainStore
from src.mcp.domains.tickets_module import TicketsDomainStore
from src.mcp.server import CareMCPServer
from src.planner.planner import DryRunPlanner
from src.policy.engine import PolicyEngine
from src.journal.db import ActionJournalDB
from src.executor.runner import ControlledExecutor
from src.schemas.plan import (
    PlanStatus,
    PlannedAction,
    CompensationAction,
    CandidatePlan,
    RiskLevel,
    PolicyOutcome,
)
from src.schemas.intent import StructuredIntent, IntentConfidence, IntentConstraint, ConstraintType
from src.verifier.invariant_checker import InvariantChecker
from src.recovery.compensation import SagaCompensationRunner
from src.integrity.hasher import compute_action_hash


def banner(title: str):
    print(f"\n{'='*25} {title} {'='*25}")


def scenario_a_clear_request():
    banner("SCENARIO A — CLEAR REQUEST")
    print("User Request: 'Move 3 PM meeting to 4 PM'")
    print("Expected: LLM -> StructuredIntent -> Planner -> Policy (AUTO_APPROVE) -> Journal -> FastMCP -> Execution -> Verification -> Success")

    calendar_store = CalendarDomainStore()
    calendar_store.reset()
    mcp = CareMCPServer(calendar_store=calendar_store)
    journal = ActionJournalDB()
    executor = ControlledExecutor(journal_db=journal, mcp_server=mcp)
    parser = IntentParser()
    planner = DryRunPlanner(mcp_server=mcp)
    policy = PolicyEngine(mcp_server=mcp)
    verifier = InvariantChecker()

    # 1. Intent Parsing
    prompt = "Move 3 PM meeting to 4 PM"
    intent = parser.parse(prompt)
    print(f"[1. LLM Intent Extracted]: Goal='{intent.goal}', Confidence='{intent.intent_confidence.value}', Ambiguities={intent.ambiguities}")
    assert intent.scope == "calendar"
    assert len(intent.ambiguities) == 0

    # 2. Planner Dry Run
    plan = planner.generate_candidate_plan(intent=intent, actor="user_mithun", user_role="STANDARD_USER")
    print(f"[2. Planner Dry Run]: Target={plan.actions[0].resource_id}, Operation={plan.actions[0].operation}, ActionHash={plan.action_hash[:16]}...")
    assert len(plan.actions) == 1
    assert plan.actions[0].parameters["start_time"] == "2026-10-02T16:00:00Z"

    # 3. Policy Evaluation
    decision = policy.evaluate(plan)
    print(f"[3. Policy Engine]: Outcome={decision.outcome.value.upper()}, Reason='{decision.reason}'")
    assert decision.outcome == PolicyOutcome.AUTO_APPROVE

    # 4. Save and Execute Plan
    plan.policy_outcome = decision.outcome
    plan.status = PlanStatus.APPROVED
    journal.save_approved_plan(plan)

    exec_result = executor.execute_plan(plan.plan_id, plan.action_hash)
    print(f"[4. Controlled Execution]: Status={exec_result['status']}, ExecutedActions={exec_result['executed_actions']}")
    assert exec_result["status"] == "done"

    # 5. State Verification
    event_after = mcp.get_calendar_event("evt_3pm_sync")
    print(f"[5. Verification]: Live start_time='{event_after['start_time']}'")
    assert event_after["start_time"] == "2026-10-02T16:00:00Z"

    inv_result = verifier.verify_plan(plan, exec_result["observed_states"])
    print(f"[6. Invariant Checks]: Passed={inv_result.passed}")
    assert inv_result.passed

    print(">>> SCENARIO A PASSED: Full clear request lifecycle executed cleanly.")


def scenario_b_main_hackathon_ambiguity():
    banner("SCENARIO B — MAIN HACKATHON SCENARIO (CLEAR MY AFTERNOON)")
    print("User Request: 'Clear my afternoon so I can finish the proposal.'")
    print("Expected: Planner resolves actual calendar events -> Policy detects ambiguity / external client meeting -> CLARIFY (Zero blind cancellations).")

    calendar_store = CalendarDomainStore()
    calendar_store.reset()
    mcp = CareMCPServer(calendar_store=calendar_store)
    parser = IntentParser()
    planner = DryRunPlanner(mcp_server=mcp)
    policy = PolicyEngine(mcp_server=mcp)

    prompt = "Clear my afternoon so I can finish the proposal"
    intent = parser.parse(prompt)
    print(f"[1. LLM Intent Extracted]: Goal='{intent.goal}', Ambiguities={intent.ambiguities}")

    # Inspect seed calendar state
    all_events = mcp.list_calendar_events()
    print(f"[2. Live Calendar]: {len(all_events)} events total on 2026-10-02:")
    for ev in all_events:
        has_ext = any(att.get("is_external", False) for att in ev.get("attendees", []))
        print(f"   • {ev['id']} ({ev['start_time']}): '{ev['title']}' | External Attendee: {has_ext}")

    # Planner dry run resolves affected events
    plan = planner.generate_candidate_plan(intent=intent)
    print(f"[3. Planner Dry Run]: Discovered {len(plan.actions)} candidate afternoon events:")
    for act in plan.actions:
        print(f"   • {act.resource_id} (Risk: {act.risk_level.value})")

    # Policy evaluation
    decision = policy.evaluate(plan)
    print(f"[4. Deterministic Policy Gate]: Outcome={decision.outcome.value.upper()}")
    print(f"   Reason: {decision.reason}")
    assert decision.outcome == PolicyOutcome.CLARIFY

    # Verify zero mutations occurred
    assert mcp.get_calendar_event("evt_client_review")["status"] == "confirmed"
    assert "[CANCELLED]" not in mcp.get_calendar_event("evt_client_review")["title"]
    print(">>> SCENARIO B PASSED: Unstated assumption intercepted; external client meeting safely preserved.")


def scenario_c_consequential_action():
    banner("SCENARIO C — CONSEQUENTIAL ACTION REQUIRING CONFIRM")
    print("User Request: 'Close escalated ticket #402'")
    print("Expected: High LLM confidence does NOT bypass policy -> Deterministic Policy requires CONFIRM -> User approves -> Execution.")

    tickets_store = TicketsDomainStore()
    tickets_store.reset()
    mcp = CareMCPServer(tickets_store=tickets_store)
    journal = ActionJournalDB()
    executor = ControlledExecutor(journal_db=journal, mcp_server=mcp)
    parser = IntentParser()
    planner = DryRunPlanner(mcp_server=mcp)
    policy = PolicyEngine(mcp_server=mcp)

    prompt = "Close escalated ticket #402"
    intent = parser.parse(prompt)
    print(f"[1. LLM Intent Extracted]: Goal='{intent.goal}', LLM Confidence={intent.intent_confidence.value}")

    plan = planner.generate_candidate_plan(intent=intent, actor="user_mithun", user_role="STANDARD_USER")
    decision = policy.evaluate(plan)

    print(f"[2. Policy Gate]: Outcome={decision.outcome.value.upper()} (Authority: Deterministic Policy)")
    print(f"   Reason: {decision.reason}")
    assert decision.outcome == PolicyOutcome.CONFIRM

    # Simulate explicit human confirmation
    print("[3. User Interaction]: User clicks [Confirm Escalation Closure]")
    plan.policy_outcome = PolicyOutcome.CONFIRM
    plan.status = PlanStatus.APPROVED
    journal.save_approved_plan(plan, explicit_confirmation=True)

    exec_result = executor.execute_plan(plan.plan_id, plan.action_hash)
    print(f"[4. Execution]: Status={exec_result['status']}")
    assert exec_result["status"] == "done"

    tkt = mcp.get_ticket("tkt_402")
    assert tkt["status"] == "closed"
    print(f"[5. Live Ticket]: Status='{tkt['status']}'")
    print(">>> SCENARIO C PASSED: Escalated action safely intercepted by deterministic policy gate.")


def scenario_d_ambiguous_request():
    banner("SCENARIO D — AMBIGUOUS REQUEST REACHES CLARIFY")
    print("User Request: 'Do something about my afternoon calendar later'")
    print("Expected: LLM -> Ambiguity / Low Confidence -> Zero guessed tool executions -> CLARIFY.")

    mcp = CareMCPServer()
    parser = IntentParser()
    planner = DryRunPlanner(mcp_server=mcp)
    policy = PolicyEngine(mcp_server=mcp)

    prompt = "Do something about my afternoon calendar later"
    intent = parser.parse(prompt)
    print(f"[1. LLM Intent Extracted]: Ambiguities={intent.ambiguities}, Confidence={intent.intent_confidence.value}")

    plan = planner.generate_candidate_plan(intent)
    decision = policy.evaluate(plan)

    print(f"[2. Policy Gate]: Outcome={decision.outcome.value.upper()}")
    print(f"   Reason: {decision.reason}")
    assert decision.outcome == PolicyOutcome.CLARIFY
    assert len(plan.actions) == 0 or len(intent.ambiguities) > 0
    print(">>> SCENARIO D PASSED: System safely halted at CLARIFY without guessing.")


def scenario_e_partial_failure_and_recovery():
    banner("SCENARIO E — PARTIAL FAILURE & SAGA RECOVERY")
    print("Workflow: Action 1 succeeds -> Action 2 fails -> Reverse Saga Compensation -> Verification.")

    calendar_store = CalendarDomainStore()
    calendar_store.reset()
    tickets_store = TicketsDomainStore()
    tickets_store.reset()
    mcp = CareMCPServer(calendar_store=calendar_store, tickets_store=tickets_store)
    journal = ActionJournalDB()
    executor = ControlledExecutor(journal_db=journal, mcp_server=mcp)
    recovery_runner = SagaCompensationRunner(journal_db=journal, mcp_server=mcp)

    evt_init = mcp.get_calendar_event("evt_3pm_sync")
    tkt_init = mcp.get_ticket("tkt_105")

    plan_id = f"plan_saga_{uuid.uuid4().hex[:8]}"
    actions = [
        PlannedAction(
            action_id="act_cal_step1",
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
            action_id="act_tkt_step2",
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
        intent=StructuredIntent(goal="Batch shift", scope="general"),
        actor="user_mithun",
        user_role="ADMIN",
        actions=actions,
        action_hash=action_hash,
        policy_outcome=PolicyOutcome.AUTO_APPROVE,
        status=PlanStatus.APPROVED,
    )
    journal.save_approved_plan(plan)

    print("[1. Dispatching Plan with Simulated Failure at Action 2 (tickets.update_status)]...")
    try:
        executor.execute_plan(
            plan.plan_id,
            plan.action_hash,
            simulate_failure_at_action_index=1,
            auto_compensate_on_failure=True,
        )
        assert False, "Execution should have failed at Action 2"
    except RuntimeError as exc:
        print(f"[2. Failure Intercepted & Saga Triggered]: {exc}")
        assert "compensated" in str(exc).lower() or "success" in str(exc).lower()

    # Verify calendar is restored to initial state
    final_event = mcp.get_calendar_event("evt_3pm_sync")
    print(f"[3. Live Verification]: Calendar start_time='{final_event['start_time']}' (Restored to before_state: '{evt_init['start_time']}')")
    assert final_event["start_time"] == evt_init["start_time"]
    print(">>> SCENARIO E PASSED: Partial failure cleanly rolled back via drift-checked saga compensation.")


def main():
    banner("CARE FRAMEWORK — COMPLETE END-TO-END SCENARIO VALIDATION")
    scenario_a_clear_request()
    scenario_b_main_hackathon_ambiguity()
    scenario_c_consequential_action()
    scenario_d_ambiguous_request()
    scenario_e_partial_failure_and_recovery()
    banner("ALL 5 END-TO-END SCENARIOS VALIDATED SUCCESSFULLY")


if __name__ == "__main__":
    main()
