import pytest
from src.agent.care_agent import CareAgent, AgentResponse
from src.mcp.domains.calendar_module import CalendarDomainStore
from src.mcp.domains.tickets_module import TicketsDomainStore
from src.mcp.server import CareMCPServer
from src.journal.db import ActionJournalDB
from src.executor.runner import ControlledExecutor
from src.intent.parser import IntentParser
from src.planner.planner import DryRunPlanner
from src.policy.engine import PolicyEngine
from src.schemas.plan import PolicyOutcome, PlanStatus


@pytest.fixture
def agent_fixture(tmp_path):
    cal_store = CalendarDomainStore()
    tkt_store = TicketsDomainStore()
    mcp = CareMCPServer(calendar_store=cal_store, tickets_store=tkt_store)
    journal = ActionJournalDB(db_path=str(tmp_path / "journal.db"))
    executor = ControlledExecutor(journal_db=journal, mcp_server=mcp)
    parser = IntentParser(use_cache=False)
    planner = DryRunPlanner(mcp_server=mcp)
    policy = PolicyEngine(mcp_server=mcp)
    agent = CareAgent(
        mcp_server=mcp,
        journal_db=journal,
        executor=executor,
        parser=parser,
        planner=planner,
        policy_engine=policy,
    )
    return agent, mcp, journal


def test_agent_general_conversational_question(agent_fixture):
    agent, mcp, _ = agent_fixture
    response = agent.process_message("Explain what CARE stands for and how it works")
    assert "CARE" in response.text
    assert response.plan is None  # Does not attempt invalid execution


def test_agent_create_new_calendar_event(agent_fixture):
    agent, mcp, _ = agent_fixture
    response = agent.process_message("Schedule a meeting with Alice at 4 PM")
    assert response.decision is not None
    assert response.decision.outcome == PolicyOutcome.AUTO_APPROVE
    assert "Meeting Scheduled Successfully" in response.text
    # Verify event exists in live calendar store
    events = mcp.list_calendar_events()
    created = [e for e in events if "alice" in e.get("title", "").lower()]
    assert len(created) >= 1


def test_agent_cancel_calendar_event(agent_fixture):
    agent, mcp, _ = agent_fixture
    # evt_3pm_sync is in seed data at 15:00
    response = agent.process_message("Cancel my 3 PM meeting")
    assert response.decision is not None
    assert response.decision.outcome == PolicyOutcome.AUTO_APPROVE
    assert "Meeting Cancelled Successfully" in response.text
    ev = mcp.get_calendar_event("evt_3pm_sync")
    assert "[CANCELLED]" in ev["title"]


def test_agent_create_ticket(agent_fixture):
    agent, mcp, _ = agent_fixture
    response = agent.process_message("Create ticket for auth service timeout")
    assert response.decision is not None
    assert response.decision.outcome == PolicyOutcome.AUTO_APPROVE
    assert "Ticket Filed Successfully" in response.text
    tickets = mcp.list_tickets()
    created = [t for t in tickets if "auth service timeout" in t.get("title", "").lower()]
    assert len(created) >= 1


def test_agent_ticket_status_in_progress(agent_fixture):
    agent, mcp, _ = agent_fixture
    response = agent.process_message("Mark ticket 105 in progress")
    assert response.decision is not None
    assert response.decision.outcome == PolicyOutcome.AUTO_APPROVE
    assert "Ticket Status Updated" in response.text
    tkt = mcp.get_ticket("tkt_105")
    assert tkt["status"] == "in_progress"


def test_agent_unresolved_calendar_target_gives_helpful_clarification(agent_fixture):
    agent, mcp, _ = agent_fixture
    response = agent.process_message("Move meeting with NonExistentPerson to 5 PM")
    assert "couldn't locate a match" in response.text
    assert "Here is your current schedule" in response.text
    assert len(response.suggested_actions) > 0


def test_agent_schedule_event_conflict_clarifies(agent_fixture):
    agent, mcp, _ = agent_fixture
    # 3 PM is occupied by evt_3pm_sync in seed calendar
    response = agent.process_message("Schedule an event... client meeting for 3 pm today")
    assert response.decision is not None
    assert response.decision.outcome == PolicyOutcome.CLARIFY
    assert "Target slot is occupied by 'Weekly Team Sync'" in response.text
    assert response.needs_clarification is True
    assert len(response.suggested_actions) > 0


def test_agent_schedule_event_free_slot_succeeds(agent_fixture):
    agent, mcp, _ = agent_fixture
    # 4 PM is free
    response = agent.process_message("Schedule an event... client meeting for 4 pm today")
    assert response.decision is not None
    assert response.decision.outcome == PolicyOutcome.AUTO_APPROVE
    assert "Meeting Scheduled Successfully" in response.text
    assert response.execution_result["status"] == "done"
    events = mcp.list_calendar_events()
    created = [e for e in events if "client meeting" in e.get("title", "").lower()]
    assert len(created) >= 1

