import tempfile
from pathlib import Path

import pytest

from src.integrity.hasher import compute_action_hash
from src.intent.parser import IntentParser
from src.journal.db import ActionJournalDB
from src.mcp.domains.calendar_module import CalendarDomainStore
from src.mcp.server import CareMCPServer
from src.planner.planner import DryRunPlanner
from src.schemas.intent import IntentConstraint, ConstraintType, StructuredIntent
from src.schemas.plan import (
    PlanStatus, PolicyOutcome, CandidatePlan, PlannedAction,
    CompensationAction, RiskLevel,
)
from src.executor.runner import ControlledExecutor
from src.recovery.compensation import SagaCompensationRunner


def _setup():
    tmp = tempfile.TemporaryDirectory()
    mcp = CareMCPServer(calendar_store=CalendarDomainStore())
    journal = ActionJournalDB(Path(tmp.name) / "journal.db")
    return tmp, mcp, journal


def test_approved_record_rejects_non_executable_policy_outcome():
    tmp, mcp, journal = _setup()
    try:
        plan = DryRunPlanner(mcp).generate_candidate_plan(IntentParser(False).parse("Move 3 PM meeting to 4 PM"))
        plan.policy_outcome = PolicyOutcome.BLOCK
        plan.status = PlanStatus.APPROVED
        with pytest.raises(ValueError, match="Only auto-approved"):
            journal.save_approved_plan(plan)
        plan.policy_outcome = PolicyOutcome.CONFIRM
        with pytest.raises(ValueError, match="explicit human confirmation"):
            journal.save_approved_plan(plan)
    finally:
        tmp.cleanup()


def test_executor_verifies_invariants_and_compensates_failure():
    tmp, mcp, journal = _setup()
    try:
        plan = DryRunPlanner(mcp).generate_candidate_plan(IntentParser(False).parse("Move 3 PM meeting to 4 PM"))
        plan.actions[0].constraints.append(IntentConstraint(
            type=ConstraintType.EXPECTED_STATE_EQUALS_ACTUAL_STATE,
            params={"expected": {"start_time": "2099-01-01T00:00:00Z"}},
            source="explicit",
        ))
        plan.action_hash = compute_action_hash(plan.actions)
        plan.policy_outcome = PolicyOutcome.AUTO_APPROVE
        plan.status = PlanStatus.APPROVED
        journal.save_approved_plan(plan)
        with pytest.raises(RuntimeError, match="invariant verification failed"):
            ControlledExecutor(journal, mcp).execute_plan(plan.plan_id, plan.action_hash)
        assert mcp.get_calendar_event("evt_3pm_sync")["start_time"] == plan.actions[0].before_state["start_time"]
        assert journal.get_plan(plan.plan_id)["status"] == PlanStatus.COMPENSATED.value
    finally:
        tmp.cleanup()


def test_unresolved_calendar_target_does_not_fall_back_to_first_event():
    tmp, mcp, _ = _setup()
    try:
        intent = IntentParser(False).parse("Move the 11 AM meeting to 2 PM")
        plan = DryRunPlanner(mcp).generate_candidate_plan(intent)
        assert plan.actions == []
        unresolved_ticket = IntentParser(False).parse("Close ticket #999")
        ticket_plan = DryRunPlanner(mcp).generate_candidate_plan(unresolved_ticket)
        assert ticket_plan.actions == []
    finally:
        tmp.cleanup()


def test_explicit_reschedule_date_is_used():
    tmp, mcp, _ = _setup()
    try:
        intent = IntentParser(False).parse("Move my 3 PM meeting to 4 PM on 2026-10-04")
        plan = DryRunPlanner(mcp).generate_candidate_plan(intent)
        assert plan.actions[0].parameters["start_time"] == "2026-10-04T16:00:00Z"
    finally:
        tmp.cleanup()


def test_stale_later_action_compensates_prior_independent_action():
    tmp, mcp, journal = _setup()
    try:
        actions = [
            PlannedAction(
                action_id="first", resource_type="tickets", resource_id="tkt_105",
                operation="tickets.update_status",
                parameters={"ticket_id": "tkt_105", "new_status": "closed"},
                before_state=mcp.get_ticket("tkt_105"),
                compensation_action=CompensationAction(
                    operation="tickets.update_status",
                    parameters={"ticket_id": "tkt_105", "new_status": "open"},
                ),
            ),
            PlannedAction(
                action_id="second", resource_type="tickets", resource_id="tkt_402",
                operation="tickets.update_status",
                parameters={"ticket_id": "tkt_402", "new_status": "closed"},
                before_state=mcp.get_ticket("tkt_402"),
                compensation_action=CompensationAction(
                    operation="tickets.update_status",
                    parameters={"ticket_id": "tkt_402", "new_status": "in_progress"},
                ),
            ),
        ]
        plan = CandidatePlan(
            plan_id="stale-second-action", intent=StructuredIntent(goal="Close tickets", scope="tickets"),
            actor="user", user_role="ADMIN", actions=actions,
            action_hash=compute_action_hash(actions), policy_outcome=PolicyOutcome.CONFIRM,
            status=PlanStatus.APPROVED,
        )
        journal.save_approved_plan(plan, explicit_confirmation=True)
        executor = ControlledExecutor(journal, mcp)
        original_check = executor.freshness_checker.check_freshness
        checks = 0

        def mutate_before_second_check(**kwargs):
            nonlocal checks
            checks += 1
            if checks == 2:
                mcp.tickets_store.tickets["tkt_402"]["assigned_to"] = "external_editor"
            return original_check(**kwargs)

        executor.freshness_checker.check_freshness = mutate_before_second_check
        with pytest.raises(RuntimeError, match="STALE_RESOURCE_STATE"):
            executor.execute_plan(plan.plan_id, plan.action_hash)
        assert mcp.get_ticket("tkt_105") == actions[0].before_state
        assert mcp.get_ticket("tkt_402")["assigned_to"] == "external_editor"
        assert journal.get_plan(plan.plan_id)["status"] == PlanStatus.COMPENSATED.value
    finally:
        tmp.cleanup()


def test_recovery_reconciles_interrupted_action_before_compensating():
    tmp, mcp, journal = _setup()
    try:
        before = mcp.get_ticket("tkt_105")
        action = PlannedAction(
            action_id="interrupted", resource_type="tickets", resource_id="tkt_105",
            operation="tickets.update_status",
            parameters={"ticket_id": "tkt_105", "new_status": "closed", "resolution_notes": "planned"},
            before_state=before,
            compensation_action=CompensationAction(
                operation="tickets.update_status",
                parameters={"ticket_id": "tkt_105", "new_status": "open", "clear_resolution_notes": True},
            ),
        )
        plan = CandidatePlan(
            plan_id="interrupted-plan", intent=StructuredIntent(goal="Close ticket", scope="tickets"),
            actor="user", user_role="STANDARD_USER", actions=[action],
            action_hash=compute_action_hash([action]), policy_outcome=PolicyOutcome.AUTO_APPROVE,
            status=PlanStatus.APPROVED,
        )
        journal.save_approved_plan(plan)
        record_id = journal.log_pre_action(
            plan_id=plan.plan_id, action_id=action.action_id, action_hash=plan.action_hash,
            actor=plan.actor, user_role=plan.user_role, resource_type=action.resource_type,
            resource_id=action.resource_id, operation=action.operation,
            before_state=before, compensation_action=action.compensation_action.model_dump(),
        )
        mcp.dispatch_tool(action.operation, action.parameters, plan.plan_id, plan.action_hash)

        result = SagaCompensationRunner(journal, mcp).compensate_plan(plan.plan_id)
        assert result.status == "compensated"
        assert mcp.get_ticket("tkt_105") == before
        assert journal.get_journal_records(plan.plan_id)[0].status.value == "compensated"
    finally:
        tmp.cleanup()
