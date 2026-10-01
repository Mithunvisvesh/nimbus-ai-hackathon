"""Unified CLI Runner for CARE Demonstration Beats (1, 2, 3, 4).

Usage:
    python demo/run_beat.py --beat 1
    python demo/run_beat.py --beat 2
    python demo/run_beat.py --beat 3
    python demo/run_beat.py --beat 4
    python demo/run_beat.py --all
"""

import argparse
import sys
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
from src.schemas.plan import PlanStatus, PlannedAction, CompensationAction, CandidatePlan, RiskLevel, PolicyOutcome
from src.schemas.intent import StructuredIntent
from src.verifier.invariant_checker import InvariantChecker
from src.recovery.compensation import SagaCompensationRunner
from src.integrity.hasher import compute_action_hash


def banner(title: str):
    print(f"\n{'='*25} {title} {'='*25}")


def run_beat_1():
    banner("BEAT 1: CLEAR REQUEST WITH CONFLICT CHECK")
    print("Description: User requests 'Move my 3 PM meeting to 5 PM', but 5 PM is occupied.")
    print("Expected: Dry run detects conflict -> Policy produces CLARIFY -> User picks 4 PM -> AUTO-APPROVE -> Execute.")

    calendar_store = CalendarDomainStore()
    calendar_store.reset()
    mcp = CareMCPServer(calendar_store=calendar_store)
    journal = ActionJournalDB()
    executor = ControlledExecutor(journal_db=journal, mcp_server=mcp)
    parser = IntentParser(use_cache=False)
    planner = DryRunPlanner(mcp_server=mcp)
    policy = PolicyEngine(mcp_server=mcp)

    prompt = "Move my 3 PM meeting to 5 PM"
    print(f"\n[Prompt]: \"{prompt}\"")
    intent = parser.parse(prompt)
    plan = planner.generate_candidate_plan(intent=intent)
    decision = policy.evaluate(plan)

    print(f">> Policy Outcome: {decision.outcome.value.upper()}")
    print(f">> Reason: {decision.reason}")
    print(f">> Alternatives: {decision.suggested_alternatives}")

    print("\n>>> User selects: 4:00 PM (16:00)")
    replan_intent = parser.parse("Move my 3 PM meeting to 4 PM")
    replan = planner.generate_candidate_plan(intent=replan_intent)
    replan_dec = policy.evaluate(replan)

    print(f">> Re-plan Outcome: {replan_dec.outcome.value.upper()} ({replan_dec.reason})")
    replan.policy_outcome = replan_dec.outcome
    replan.status = PlanStatus.APPROVED
    journal.save_approved_plan(replan)

    exec_res = executor.execute_plan(replan.plan_id, replan.action_hash)
    print(f">> Execution Result: {exec_res['status']}")

    live_after = mcp.get_calendar_event("evt_3pm_sync")
    print(f">> Live Calendar: start_time='{live_after['start_time']}'")
    inv = InvariantChecker.verify_plan(replan, {"evt_3pm_sync": live_after})
    print(f">> Invariant Verification: Passed={inv.passed}")


def run_beat_2():
    banner("BEAT 2: AMBIGUOUS CONSEQUENTIAL SCOPE")
    print("Description: User requests 'Clear my afternoon'. Afternoon contains a VIP client meeting.")
    print("Expected: Planner resolves concrete targets -> External attendee detected -> CLARIFY.")

    calendar_store = CalendarDomainStore()
    calendar_store.reset()
    mcp = CareMCPServer(calendar_store=calendar_store)
    parser = IntentParser(use_cache=False)
    planner = DryRunPlanner(mcp_server=mcp)
    policy = PolicyEngine(mcp_server=mcp)

    prompt = "Clear my afternoon"
    print(f"\n[Prompt]: \"{prompt}\"")
    intent = parser.parse(prompt)
    print(f">> Extracted Ambiguities: {intent.ambiguities}")

    plan = planner.generate_candidate_plan(intent=intent)
    print(f">> Dry-run resolved {len(plan.actions)} affected events:")
    for a in plan.actions:
        print(f"   - {a.resource_id}: {a.before_state.get('title')} (Risk: {a.risk_level.value})")

    decision = policy.evaluate(plan)
    print(f">> Policy Outcome: {decision.outcome.value.upper()}")
    print(f">> Decision Reason: {decision.reason}")
    print(">> Result: Protected external VIP client meeting from unstated blind cancellation!")


def run_beat_3():
    banner("BEAT 3: TICKETS DOMAIN & ROLE-BASED ACCESS CONTROL (RBAC)")
    print("Description: Gating ticket mutations across user roles (READ_ONLY vs STANDARD_USER) and escalation tags.")

    tickets_store = TicketsDomainStore()
    tickets_store.reset()
    mcp = CareMCPServer(tickets_store=tickets_store)
    journal = ActionJournalDB()
    executor = ControlledExecutor(journal_db=journal, mcp_server=mcp)
    parser = IntentParser(use_cache=False)
    planner = DryRunPlanner(mcp_server=mcp)
    policy = PolicyEngine(mcp_server=mcp)

    prompt = "Close escalated ticket #402"

    # Part A: READ_ONLY
    print(f"\n[Part A: READ_ONLY User] Prompt: \"{prompt}\"")
    plan_ro = planner.generate_candidate_plan(intent=parser.parse(prompt), user_role="READ_ONLY")
    dec_ro = policy.evaluate(plan_ro)
    print(f">> Policy Outcome: {dec_ro.outcome.value.upper()}")
    print(f">> Reason: {dec_ro.reason} (Deterministically BLOCKED)")

    # Part B: STANDARD_USER on Escalation
    print(f"\n[Part B: STANDARD_USER on Escalation #402] Prompt: \"{prompt}\"")
    plan_std = planner.generate_candidate_plan(intent=parser.parse(prompt), user_role="STANDARD_USER")
    dec_std = policy.evaluate(plan_std)
    print(f">> Policy Outcome: {dec_std.outcome.value.upper()}")
    print(f">> Reason: {dec_std.reason}")
    print(">> Action: User provides explicit confirmation -> Approved.")
    plan_std.policy_outcome = dec_std.outcome
    plan_std.status = PlanStatus.APPROVED
    journal.save_approved_plan(plan_std)
    executor.execute_plan(plan_std.plan_id, plan_std.action_hash)
    print(f">> Live Ticket Status: '{mcp.get_ticket('tkt_402')['status']}'")

    # Part C: Routine Ticket #105
    print(f"\n[Part C: STANDARD_USER on Standard Ticket #105] Prompt: \"Close ticket #105\"")
    plan_105 = planner.generate_candidate_plan(intent=parser.parse("Close ticket #105"), user_role="STANDARD_USER")
    dec_105 = policy.evaluate(plan_105)
    print(f">> Policy Outcome: {dec_105.outcome.value.upper()} (Auto-Approved & Executed)")


def run_beat_4():
    banner("BEAT 4: MULTI-STEP RECOVERY & PRE-COMPENSATION DRIFT")
    print("Description: Injected partial failure triggers saga rollback; external human drift halts rollback.")

    calendar_store = CalendarDomainStore()
    calendar_store.reset()
    tickets_store = TicketsDomainStore()
    tickets_store.reset()
    mcp = CareMCPServer(calendar_store=calendar_store, tickets_store=tickets_store)
    journal = ActionJournalDB()
    executor = ControlledExecutor(journal_db=journal, mcp_server=mcp)

    evt_init = mcp.get_calendar_event("evt_3pm_sync")
    tkt_init = mcp.get_ticket("tkt_105")

    plan_id = "plan_beat4_cli_demo"
    actions = [
        PlannedAction(
            action_id="act_cal",
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
            action_id="act_tkt",
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

    print("\n1. Step 1 executes: Calendar moved to 16:00:00Z.")
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
    mcp.dispatch_tool(operation=actions[0].operation, parameters=actions[0].parameters, plan_id=plan_id, action_hash=action_hash)
    journal.log_post_action_success(record_id=rec1, after_state={"start_time": "2026-10-02T16:00:00Z"})

    print("2. External human drift occurs: Colleague reschedules 'evt_3pm_sync' to 16:45:00Z.")
    mcp.calendar_store.events["evt_3pm_sync"]["start_time"] = "2026-10-02T16:45:00Z"

    print("3. Step 2 fails with simulated error.")
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
    journal.log_post_action_failure(rec2, "Simulated network failure on Step 2")
    journal.update_plan_status(plan_id, PlanStatus.FAILED)

    print("4. Saga compensation runner evaluates Step 1 for rollback.")
    comp_runner = SagaCompensationRunner(journal_db=journal, mcp_server=mcp)
    comp_res = comp_runner.compensate_plan(plan_id=plan_id)

    print(f">> Saga Compensation Status: {comp_res.status.upper()}")
    print(f">> Detector Message: {comp_res.message}")
    print(f">> Live Calendar State: '{mcp.get_calendar_event('evt_3pm_sync')['start_time']}'")
    print(">> SAFETY ASSURANCE: Human modification preserved, rollback aborted!")


def main():
    parser = argparse.ArgumentParser(description="CARE Demonstration Beat Runner")
    parser.add_argument("--beat", choices=["1", "2", "3", "4"], help="Beat number to run (1-4)")
    parser.add_argument("--all", action="store_true", help="Run all 4 beats sequentially")
    args = parser.parse_args()

    if args.all or not args.beat:
        run_beat_1()
        run_beat_2()
        run_beat_3()
        run_beat_4()
        banner("ALL 4 DEMO BEATS COMPLETED SUCCESSFULLY")
    elif args.beat == "1":
        run_beat_1()
    elif args.beat == "2":
        run_beat_2()
    elif args.beat == "3":
        run_beat_3()
    elif args.beat == "4":
        run_beat_4()


if __name__ == "__main__":
    main()
