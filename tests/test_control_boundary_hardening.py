import tempfile
import asyncio
import secrets
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
import src.executor.runner as executor_module
from src.recovery.compensation import SagaCompensationRunner
from src.mcp.fastmcp_server import create_fastmcp_server
from fastmcp import Client


def _setup():
    tmp = tempfile.TemporaryDirectory()
    mcp = CareMCPServer(calendar_store=CalendarDomainStore())
    journal = ActionJournalDB(Path(tmp.name) / "journal.db")
    mcp.bind_journal(journal)
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
        journal.transition_plan_to_executing(plan.plan_id)
        record_id = journal.log_pre_action(
            plan_id=plan.plan_id, action_id=action.action_id, action_hash=plan.action_hash,
            actor=plan.actor, user_role=plan.user_role, resource_type=action.resource_type,
            resource_id=action.resource_id, operation=action.operation,
            before_state=before, compensation_action=action.compensation_action.model_dump(),
        )
        dispatch_token = secrets.token_urlsafe(32)
        journal.prepare_tool_dispatch(record_id, dispatch_token)
        mcp.dispatch_tool(action.operation, action.parameters, plan.plan_id, plan.action_hash, action_id=action.action_id, dispatch_token=dispatch_token)

        result = SagaCompensationRunner(journal, mcp).compensate_plan(plan.plan_id)
        assert result.status == "compensated"
        assert mcp.get_ticket("tkt_105") == before
        assert journal.get_journal_records(plan.plan_id)[0].status.value == "compensated"
    finally:
        tmp.cleanup()


def test_lost_mcp_response_reconciles_and_compensates_completed_mutation(monkeypatch):
    tmp, mcp, journal = _setup()
    try:
        before = mcp.get_ticket("tkt_105")
        action = PlannedAction(
            action_id="lost-response-action", resource_type="tickets", resource_id="tkt_105",
            operation="tickets.update_status",
            parameters={"ticket_id": "tkt_105", "new_status": "closed"},
            before_state=before,
            compensation_action=CompensationAction(
                operation="tickets.update_status",
                parameters={"ticket_id": "tkt_105", "new_status": before["status"]},
            ),
        )
        plan = CandidatePlan(
            plan_id="lost-response-plan",
            intent=StructuredIntent(goal="Close ticket", scope="tickets"),
            actor="user", user_role="ADMIN", actions=[action],
            action_hash=compute_action_hash([action]),
            policy_outcome=PolicyOutcome.AUTO_APPROVE, status=PlanStatus.APPROVED,
        )
        journal.save_approved_plan(plan)
        executor = ControlledExecutor(journal, mcp)
        original_call = executor_module.call_fastmcp_tool_sync
        call_count = 0

        def lose_first_response(protocol, name, arguments):
            nonlocal call_count
            call_count += 1
            result = original_call(protocol, name, arguments)
            if call_count == 1:
                raise executor_module.UnknownMCPOutcomeError("simulated lost MCP response")
            return result

        monkeypatch.setattr(executor_module, "call_fastmcp_tool_sync", lose_first_response)
        with pytest.raises(RuntimeError, match="simulated lost MCP response"):
            executor.execute_plan(plan.plan_id, plan.action_hash)

        assert call_count == 1  # The executor received the simulated lost response once.
        assert mcp.get_ticket("tkt_105") == before
        assert journal.get_plan(plan.plan_id)["status"] == PlanStatus.COMPENSATED.value
        records = journal.get_journal_records(plan.plan_id)
        assert records[0].status.value == "compensated"
    finally:
        tmp.cleanup()


def test_mutation_boundary_requires_exact_executing_action_binding():
    tmp, mcp, journal = _setup()
    try:
        executor = ControlledExecutor(journal, mcp)
        plan = DryRunPlanner(mcp).generate_candidate_plan(IntentParser(False).parse("Move 3 PM meeting to 4 PM"))
        action = plan.actions[0]
        plan.policy_outcome = PolicyOutcome.AUTO_APPROVE
        plan.status = PlanStatus.APPROVED
        journal.save_approved_plan(plan)

        before = mcp.get_calendar_event(action.resource_id)
        assert mcp.list_calendar_events()
        with pytest.raises(PermissionError):
            mcp.dispatch_tool(action.operation, action.parameters, "fabricated", "fake", action_id=action.action_id)
        with pytest.raises(PermissionError):
            mcp.calendar_store.update_event(action.resource_id, title="forged", plan_id="fake", action_hash="fake")
        assert mcp.get_calendar_event(action.resource_id) == before

        journal.transition_plan_to_executing(plan.plan_id)
        record_id = journal.log_pre_action(
            plan_id=plan.plan_id, action_id=action.action_id, action_hash=plan.action_hash,
            actor=plan.actor, user_role=plan.user_role, resource_type=action.resource_type,
            resource_id=action.resource_id, operation=action.operation,
            before_state=action.before_state, compensation_action=action.compensation_action.model_dump(),
        )
        dispatch_token = secrets.token_urlsafe(32)
        journal.prepare_tool_dispatch(record_id, dispatch_token)
        with pytest.raises(PermissionError):
            mcp.dispatch_tool(action.operation, action.parameters, plan.plan_id, "wrong", action_id=action.action_id)
        with pytest.raises(PermissionError):
            mcp.dispatch_tool("tickets.update_status", action.parameters, plan.plan_id, plan.action_hash, action_id=action.action_id)
        with pytest.raises(PermissionError):
            mcp.dispatch_tool(action.operation, {**action.parameters, "event_id": "other"}, plan.plan_id, plan.action_hash, action_id=action.action_id)
        with pytest.raises(PermissionError):
            mcp.dispatch_tool(action.operation, {**action.parameters, "title": "forged"}, plan.plan_id, plan.action_hash, action_id=action.action_id)

        result = mcp.dispatch_tool(action.operation, action.parameters, plan.plan_id, plan.action_hash, action_id=action.action_id, dispatch_token=dispatch_token)
        assert result["start_time"] == action.parameters["start_time"]
        with pytest.raises(PermissionError, match="claimed or consumed"):
            mcp.dispatch_tool(action.operation, action.parameters, plan.plan_id, plan.action_hash, action_id=action.action_id, dispatch_token=dispatch_token)
        assert executor is not None
    finally:
        tmp.cleanup()


def test_fastmcp_protocol_exposes_namespaced_discovery_and_rejects_fake_mutations():
    tmp, mcp, journal = _setup()
    try:
        protocol = create_fastmcp_server(mcp, journal)

        async def exercise_protocol():
            async with Client(protocol) as client:
                tools = await client.list_tools()
                names = {tool.name for tool in tools}
                assert "calendar.list_events" in names
                assert "calendar.update_event" in names
                assert "tickets.list_tickets" in names
                assert "care.get_tool_metadata" in names
                events = await client.call_tool("calendar.list_events", {})
                assert not events.is_error
                rejected = await client.call_tool("calendar.update_event", {
                    "plan_id": "fake", "action_hash": "fake", "action_id": "fake",
                    "event_id": "evt_3pm_sync", "start_time": "2026-10-02T16:00:00Z",
                }, raise_on_error=False)
                assert rejected.is_error

        asyncio.run(exercise_protocol())
    finally:
        tmp.cleanup()
