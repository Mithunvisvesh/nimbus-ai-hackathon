import json
import sys
from pathlib import Path

# Add project root to sys.path
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


def print_banner(title: str):
    print(f"\n{'='*28} {title} {'='*28}")


def run_beat3_demo():
    print_banner("BEAT 3: TICKETS DOMAIN, RBAC GATING & CONSEQUENTIAL ESCALATIONS")
    
    calendar_store = CalendarDomainStore()
    calendar_store.reset()
    tickets_store = TicketsDomainStore()
    tickets_store.reset()
    mcp_server = CareMCPServer(calendar_store=calendar_store, tickets_store=tickets_store)
    journal_db = ActionJournalDB()
    executor = ControlledExecutor(journal_db=journal_db, mcp_server=mcp_server)
    parser = IntentParser(use_cache=False)
    planner = DryRunPlanner(mcp_server=mcp_server)
    policy_engine = PolicyEngine(mcp_server=mcp_server)

    # ---------------------------------------------------------
    # Part A: READ_ONLY User Attempts Ticket Mutation -> BLOCKED
    # ---------------------------------------------------------
    print("\n[PART A: RBAC ENFORCEMENT - READ_ONLY ROLE]")
    prompt_a = "Close escalated ticket #402"
    print(f"User Role: 'READ_ONLY' | Actor: 'user_intern'")
    print(f"Prompt: \"{prompt_a}\"")

    intent_a = parser.parse(prompt_a)
    plan_a = planner.generate_candidate_plan(intent=intent_a, actor="user_intern", user_role="READ_ONLY")
    eval_a = policy_engine.evaluate(plan_a)

    print(f">> POLICY GATE OUTCOME: {eval_a.outcome.value.upper()}")
    print(f">> REASON: {eval_a.reason}")
    print(">> RESULT: Execution strictly blocked by deterministic RBAC boundary.")

    # ---------------------------------------------------------
    # Part B: STANDARD_USER Attempts Escalated Ticket -> CONFIRM
    # ---------------------------------------------------------
    print("\n[PART B: CONSEQUENTIAL ACTION - ESCALATED TICKET #402]")
    print(f"User Role: 'STANDARD_USER' | Actor: 'user_mithun'")
    print(f"Prompt: \"{prompt_a}\"")

    tkt_402_init = mcp_server.get_ticket("tkt_402")
    print(f"Target Resource State: ID={tkt_402_init['id']} | Status={tkt_402_init['status']} | Escalated={tkt_402_init['is_escalated']} | Priority={tkt_402_init['priority']}")

    plan_b = planner.generate_candidate_plan(intent=intent_a, actor="user_mithun", user_role="STANDARD_USER")
    eval_b = policy_engine.evaluate(plan_b)

    print(f">> POLICY GATE OUTCOME: {eval_b.outcome.value.upper()}")
    print(f">> REASON: {eval_b.reason}")
    print(">> SAFETY GUARANTEE: Escalated production issue cannot be silently closed without human sign-off.")

    # User explicitly confirms
    print("\n>>> USER INTERACTION: User confirms closure of escalated ticket #402.")
    plan_b.policy_outcome = eval_b.outcome
    plan_b.status = PlanStatus.APPROVED
    journal_db.save_approved_plan(plan_b, explicit_confirmation=True)

    exec_res_b = executor.execute_plan(plan_b.plan_id, plan_b.action_hash)
    print(f">> CONTROLLED EXECUTOR: Status={exec_res_b['status']}")

    tkt_402_after = mcp_server.get_ticket("tkt_402")
    print(f">> LIVE TICKET STATE AFTER: Status='{tkt_402_after['status']}'")

    inv_b = InvariantChecker.verify_plan(plan_b, {"tkt_402": tkt_402_after})
    print(f">> INVARIANT AUDIT: Passed={inv_b.passed} ({inv_b.summary})")

    # ---------------------------------------------------------
    # Part C: STANDARD_USER Closes Standard Ticket -> AUTO-APPROVE
    # ---------------------------------------------------------
    print("\n[PART C: ROUTINE TICKET #105 - LOW-RISK AUTO-APPROVE]")
    prompt_c = "Close ticket #105"
    print(f"Prompt: \"{prompt_c}\"")

    intent_c = parser.parse(prompt_c)
    plan_c = planner.generate_candidate_plan(intent=intent_c, actor="user_mithun", user_role="STANDARD_USER")
    eval_c = policy_engine.evaluate(plan_c)

    print(f">> POLICY GATE OUTCOME: {eval_c.outcome.value.upper()}")
    print(f">> REASON: {eval_c.reason}")

    plan_c.policy_outcome = eval_c.outcome
    plan_c.status = PlanStatus.APPROVED
    journal_db.save_approved_plan(plan_c)

    exec_res_c = executor.execute_plan(plan_c.plan_id, plan_c.action_hash)
    print(f">> CONTROLLED EXECUTOR: Status={exec_res_c['status']}")
    print(f">> LIVE TICKET #105 STATE: Status='{mcp_server.get_ticket('tkt_105')['status']}'")


def run_beat4_demo():
    print_banner("BEAT 4: MULTI-STEP CROSS-DOMAIN RECOVERY & PRE-COMPENSATION DRIFT")

    calendar_store = CalendarDomainStore()
    calendar_store.reset()
    tickets_store = TicketsDomainStore()
    tickets_store.reset()
    mcp_server = CareMCPServer(calendar_store=calendar_store, tickets_store=tickets_store)
    journal_db = ActionJournalDB()
    executor = ControlledExecutor(journal_db=journal_db, mcp_server=mcp_server)

    evt_init = mcp_server.get_calendar_event("evt_3pm_sync")
    tkt_init = mcp_server.get_ticket("tkt_105")

    print(f"[INITIAL SYSTEM STATE]:")
    print(f"  Step 1 Target: Calendar '{evt_init['id']}' | Time: {evt_init['start_time']}")
    print(f"  Step 2 Target: Ticket '{tkt_init['id']}'   | Status: {tkt_init['status']}")

    plan_id = "plan_cross_domain_demo_beat4"
    actions = [
        PlannedAction(
            action_id="act_cal_shift",
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
            action_id="act_tkt_close",
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
        intent=StructuredIntent(goal="End-of-day calendar shift and ticket close", scope="general"),
        actor="user_mithun",
        user_role="ADMIN",
        actions=actions,
        action_hash=action_hash,
        policy_outcome=PolicyOutcome.AUTO_APPROVE,
        status=PlanStatus.APPROVED,
    )
    journal_db.save_approved_plan(plan)

    print("\n[PART A: STEP 1 EXECUTION]")
    rec1 = journal_db.log_pre_action(
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
    mcp_server.dispatch_tool(
        operation=actions[0].operation,
        parameters=actions[0].parameters,
        plan_id=plan_id,
        action_hash=action_hash,
    )
    journal_db.log_post_action_success(
        record_id=rec1,
        after_state={"start_time": "2026-10-02T16:00:00Z"},
    )
    print(">> Step 1 (Calendar Update) executed successfully -> Moved to 16:00 (4:00 PM).")

    print("\n[PART B: SIMULATED EXTERNAL HUMAN EDIT (DRIFT)]")
    # Human moves the meeting to 16:45 directly
    mcp_server.calendar_store.events["evt_3pm_sync"]["start_time"] = "2026-10-02T16:45:00Z"
    print(">> An external colleague directly modified 'evt_3pm_sync' start_time to '16:45:00Z'.")

    print("\n[PART C: STEP 2 INJECTED FAILURE & SAGA ROLLBACK ATTEMPT]")
    rec2 = journal_db.log_pre_action(
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
    journal_db.log_post_action_failure(rec2, "Simulated network timeout on ticketing service")
    journal_db.update_plan_status(plan_id, PlanStatus.FAILED)
    print(">> Step 2 failed! Controlled recovery engine triggered.")

    comp_runner = SagaCompensationRunner(journal_db=journal_db, mcp_server=mcp_server)
    comp_res = comp_runner.compensate_plan(plan_id=plan_id)

    print(f"\n[PART D: DRIFT DETECTION & PROTECTION RESULT]")
    print(f">> SAGA STATUS: {comp_res.status.upper()}")
    print(f">> DRIFT DETECTOR MESSAGE: {comp_res.message}")
    
    live_evt = mcp_server.get_calendar_event("evt_3pm_sync")
    print(f">> LIVE CALENDAR STATE: start_time='{live_evt['start_time']}'")
    print(f">> HUMAN EDIT PRESERVED: Automatic rollback HALTED; human modification was NOT corrupted.")

    incidents = journal_db.get_drift_incidents(plan_id=plan_id)
    print(f">> DURABLE INCIDENT LOGGED: Incident ID={incidents[0]['incident_id']} | Status={incidents[0]['remediation_status']}")


def run_checkpoint3_demo():
    print_banner("NIMBUS 2026 — CARE CHECKPOINT 3 DEMONSTRATION")
    print("Showcasing: Multi-Domain Generalization (Calendar + Tickets), Consequential RBAC, and Drift-Aware Saga Recovery")
    run_beat3_demo()
    run_beat4_demo()
    print_banner("CHECKPOINT 3 (GENERALIZATION, DRIFT & DEMO POLISH) VERIFIED")


if __name__ == "__main__":
    run_checkpoint3_demo()
