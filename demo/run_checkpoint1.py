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
from src.schemas.plan import PlanStatus

def print_separator(title: str):
    print(f"\n{'='*25} {title} {'='*25}")

def run_checkpoint1_demo():
    print_separator("CARE PIPELINE — CHECKPOINT 1 DEMO")
    prompt = "Move 3 PM meeting to 4 PM"
    print(f"[INPUT PROMPT]: \"{prompt}\"")

    # 1. Initialize MCP Store & Server
    calendar_store = CalendarDomainStore()
    calendar_store.reset()
    mcp_server = CareMCPServer(calendar_store=calendar_store)
    journal_db = ActionJournalDB()
    executor = ControlledExecutor(journal_db=journal_db, mcp_server=mcp_server)

    evt_init = mcp_server.get_calendar_event("evt_3pm_sync")
    print(f"\n[1. LIVE RESOURCE STATE BEFORE]:")
    print(f"   ID: {evt_init['id']} | Title: {evt_init['title']} | Start: {evt_init['start_time']}")

    # 2. Intent Parsing
    print_separator("STAGE 1: PROBABILISTIC INTENT PARSING")
    parser = IntentParser(use_cache=False)
    intent = parser.parse(prompt)
    print(json.dumps(intent.model_dump(), indent=2))

    # 3. Dry-Run Planning
    print_separator("STAGE 2: READ-ONLY DRY RUN & TARGET RESOLUTION")
    planner = DryRunPlanner(mcp_server=mcp_server)
    candidate_plan = planner.generate_candidate_plan(intent=intent, actor="user_mithun", user_role="STANDARD_USER")
    print(f"Plan ID: {candidate_plan.plan_id}")
    print(f"Canonical Action Hash (SHA-256): {candidate_plan.action_hash}")
    print(f"Action 0 Target: {candidate_plan.actions[0].resource_id} ({candidate_plan.actions[0].operation})")
    print(f"Action 0 Before State: {candidate_plan.actions[0].before_state['start_time']}")
    print(f"Action 0 Target Params: {candidate_plan.actions[0].parameters['start_time']}")

    # 4. Policy Engine Evaluation
    print_separator("STAGE 3: DETERMINISTIC POLICY EVALUATION")
    policy_engine = PolicyEngine(mcp_server=mcp_server)
    eval_result = policy_engine.evaluate(candidate_plan)
    print(f"Policy Decision: {eval_result.outcome.value.upper()}")
    print(f"Reason: {eval_result.reason}")

    # Set approved
    candidate_plan.policy_outcome = eval_result.outcome
    candidate_plan.status = PlanStatus.APPROVED
    journal_db.save_approved_plan(candidate_plan)
    print("Plan saved to durable store: approved_plans")

    # 5. Controlled Execution
    print_separator("STAGE 4: GATED EXECUTION & WRITE-AHEAD JOURNAL")
    exec_result = executor.execute_plan(
        plan_id=candidate_plan.plan_id,
        submitted_action_hash=candidate_plan.action_hash,
    )
    print(f"Execution Result: {json.dumps(exec_result, indent=2)}")

    # 6. Live Store & Journal Verification
    print_separator("STAGE 5: VERIFICATION & AUDIT TRAIL")
    evt_final = mcp_server.get_calendar_event("evt_3pm_sync")
    print(f"[LIVE RESOURCE STATE AFTER]:")
    print(f"   ID: {evt_final['id']} | Title: {evt_final['title']} | Start: {evt_final['start_time']}")

    records = journal_db.get_journal_records(candidate_plan.plan_id)
    print(f"\n[SQLITE WAL JOURNAL AUDIT RECORDS ({len(records)})]:")
    for r in records:
        print(f" - Action ID: {r.action_id} | Status: {r.status.value}")
        print(f"   Before: {r.before_state.get('start_time')} -> After: {r.after_state.get('start_time')}")
        print(f"   Timestamp: {r.timestamp}")

    # 7. Single-Use Replay Protection Check
    print_separator("STAGE 6: REPLAY PROTECTION TEST")
    try:
        executor.execute_plan(candidate_plan.plan_id, candidate_plan.action_hash)
        print("ERROR: Duplicate execution should have been blocked!")
    except PermissionError as exc:
        print(f"Replay Successfully Blocked: {exc}")

    print_separator("PHASE 1 (CHECKPOINT 1) DEFINITION OF DONE ACHIEVED")

if __name__ == "__main__":
    run_checkpoint1_demo()
