import os
import pytest
from pathlib import Path
import tempfile

from src.schemas.intent import StructuredIntent, IntentConfidence
from src.schemas.plan import PolicyOutcome, PlanStatus
from src.schemas.journal import ActionJournalStatus
from src.intent.parser import IntentParser
from src.mcp.domains.calendar_module import CalendarDomainStore
from src.mcp.server import CareMCPServer
from src.planner.planner import DryRunPlanner
from src.policy.engine import PolicyEngine
from src.journal.db import ActionJournalDB
from src.executor.runner import ControlledExecutor

def test_phase1_vertical_slice():
    """
    Phase 1 Gate Criteria (DoD) Verification:
    1. Running "Move 3 PM meeting to 4 PM" generates valid JSON intent.
    2. Tool execution occurs ONLY after policy approval.
    3. SQLite database contains an audit record with populated before_state and after_state.
    4. Deterministic single-use replay protection prevents duplicate execution.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        test_db_path = Path(tmp_dir) / "test_care_journal.db"
        
        # 1. Initialize MCP Store & Server
        calendar_store = CalendarDomainStore()
        calendar_store.reset()
        mcp_server = CareMCPServer(calendar_store=calendar_store)
        journal_db = ActionJournalDB(db_path=test_db_path)
        executor = ControlledExecutor(journal_db=journal_db, mcp_server=mcp_server)

        # Initial state verification
        evt_before = mcp_server.get_calendar_event("evt_3pm_sync")
        assert evt_before is not None
        assert "15:00" in evt_before["start_time"]

        # Step A: Intent Parsing
        prompt = "Move 3 PM meeting to 4 PM"
        parser = IntentParser(use_cache=False)
        intent = parser.parse(prompt)

        assert isinstance(intent, StructuredIntent)
        assert intent.scope == "calendar"
        assert intent.intent_confidence in (IntentConfidence.MEDIUM, IntentConfidence.HIGH)
        assert len(intent.entities) >= 2

        # Step B: Read-Only Dry-Run Target Resolution
        planner = DryRunPlanner(mcp_server=mcp_server)
        candidate_plan = planner.generate_candidate_plan(
            intent=intent,
            actor="user_mithun",
            user_role="STANDARD_USER",
        )

        assert candidate_plan.plan_id.startswith("plan_")
        assert len(candidate_plan.actions) == 1
        action = candidate_plan.actions[0]
        assert action.resource_id == "evt_3pm_sync"
        assert action.operation == "calendar.update_event"
        assert "15:00" in action.before_state["start_time"]
        assert "16:00" in action.parameters["start_time"]
        assert candidate_plan.action_hash is not None
        assert len(candidate_plan.action_hash) == 64  # SHA-256 length

        # Step C: Verify Tool Execution Fails Prior to Approval
        with pytest.raises(KeyError):
            executor.execute_plan(candidate_plan.plan_id, candidate_plan.action_hash)

        # Step D: Policy Evaluation
        policy_engine = PolicyEngine(mcp_server=mcp_server)
        eval_result = policy_engine.evaluate(candidate_plan)

        assert eval_result.outcome == PolicyOutcome.AUTO_APPROVE
        candidate_plan.policy_outcome = eval_result.outcome
        candidate_plan.status = PlanStatus.APPROVED

        # Step E: Store Approved Plan in Durable Store
        journal_db.save_approved_plan(candidate_plan)
        saved_plan = journal_db.get_plan(candidate_plan.plan_id)
        assert saved_plan is not None
        assert saved_plan["status"] == PlanStatus.APPROVED.value

        # Step F: Controlled Execution (Gated with Hash Verification & Freshness)
        exec_result = executor.execute_plan(
            plan_id=candidate_plan.plan_id,
            submitted_action_hash=candidate_plan.action_hash,
        )

        assert exec_result["status"] == PlanStatus.DONE.value
        assert exec_result["executed_actions"] == 1

        # Step G: Verify State Change in Live Store
        evt_after = mcp_server.get_calendar_event("evt_3pm_sync")
        assert evt_after is not None
        assert "16:00" in evt_after["start_time"]

        # Step H: Verify SQLite Write-Ahead Journal Audit Trail
        records = journal_db.get_journal_records(candidate_plan.plan_id)
        assert len(records) == 1
        rec = records[0]
        assert rec.action_id == action.action_id
        assert rec.status == ActionJournalStatus.DONE
        assert "15:00" in rec.before_state["start_time"]
        assert rec.after_state is not None
        assert "16:00" in rec.after_state["start_time"]
        assert rec.actor == "user_mithun"
        assert rec.user_role == "STANDARD_USER"

        # Step I: Single-Use Replay Protection Verification
        # Submitting the same plan_id again must be rejected with PLAN_ALREADY_CONSUMED
        with pytest.raises(PermissionError) as exc_info:
            executor.execute_plan(candidate_plan.plan_id, candidate_plan.action_hash)
        assert "PLAN_ALREADY_CONSUMED" in str(exc_info.value)

def test_tampered_action_hash_rejection():
    """Verify that any modification to actions invalidates the hash and is rejected."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        test_db_path = Path(tmp_dir) / "test_tamper.db"
        calendar_store = CalendarDomainStore()
        mcp_server = CareMCPServer(calendar_store=calendar_store)
        journal_db = ActionJournalDB(db_path=test_db_path)
        executor = ControlledExecutor(journal_db=journal_db, mcp_server=mcp_server)

        parser = IntentParser(use_cache=False)
        intent = parser.parse("Move 3 PM meeting to 4 PM")
        planner = DryRunPlanner(mcp_server=mcp_server)
        plan = planner.generate_candidate_plan(intent=intent)
        plan.policy_outcome = PolicyOutcome.AUTO_APPROVE
        plan.status = PlanStatus.APPROVED

        journal_db.save_approved_plan(plan)

        # Attempt execution with tampered hash
        with pytest.raises(ValueError) as exc:
            executor.execute_plan(plan.plan_id, "tampered_fake_hash_1234567890")
        assert "ACTION_HASH_MISMATCH" in str(exc.value)

def test_direct_unauthorized_tool_mutation_rejection():
    """Verify that calling state-changing tool directly without plan credentials is rejected."""
    calendar_store = CalendarDomainStore()
    mcp_server = CareMCPServer(calendar_store=calendar_store)

    with pytest.raises(PermissionError):
        mcp_server.dispatch_tool(
            operation="calendar.update_event",
            parameters={"event_id": "evt_3pm_sync", "start_time": "2026-10-02T16:00:00Z"},
            plan_id="",
            action_hash="",
        )
