import pytest
from unittest.mock import MagicMock
from google.genai.errors import APIError

from src.schemas.intent import (
    StructuredIntent,
    IntentConfidence,
    IntentConstraint,
    ConstraintType,
)
from src.schemas.plan import PolicyOutcome, RiskLevel
from src.intent.providers.base import (
    LLMProvider,
    LLMProviderError,
    LLMResponseValidationError,
    LLMProviderUnavailableError,
)
from src.intent.providers.gemini_provider import GeminiProvider
from src.intent.parser import IntentParser
from src.mcp.server import CareMCPServer
from src.planner.planner import DryRunPlanner
from src.policy.engine import PolicyEngine


class MockLLMProvider(LLMProvider):
    """Deterministic mock provider for unit testing."""
    def __init__(self, response=None, side_effect=None):
        self.response = response
        self.side_effect = side_effect
        self.calls = []

    def extract_intent(self, prompt: str, user_id=None) -> StructuredIntent:
        self.calls.append({"prompt": prompt, "user_id": user_id})
        if self.side_effect:
            raise self.side_effect
        if isinstance(self.response, StructuredIntent):
            return self.response
        if isinstance(self.response, dict):
            return StructuredIntent.model_validate(self.response)
        raise ValueError("Invalid mock response configured")


# =============================================================================
# Test 1 — Valid Structured Intent
# Natural language → mocked LLM → valid StructuredIntent
# =============================================================================
def test_valid_structured_intent_extraction():
    expected_intent = StructuredIntent(
        goal="Move meeting from 3 pm to 4 pm",
        scope="calendar",
        entities=["3 pm", "4 pm", "meeting"],
        constraints=[
            IntentConstraint(
                type=ConstraintType.PRESERVE_ITEMS,
                params={"items": ["attendees", "location"]},
                source="inferred",
            ),
            IntentConstraint(
                type=ConstraintType.EXPECTED_STATE_EQUALS_ACTUAL_STATE,
                params={"property": "start_time"},
                source="policy",
            ),
        ],
        ambiguities=[],
        intent_confidence=IntentConfidence.HIGH,
    )

    mock_provider = MockLLMProvider(response=expected_intent)
    parser = IntentParser(provider=mock_provider)

    result = parser.parse("Move my 3 PM meeting to 4 PM")

    assert isinstance(result, StructuredIntent)
    assert result.goal == "Move meeting from 3 pm to 4 pm"
    assert result.scope == "calendar"
    assert result.entities == ["3 pm", "4 pm", "meeting"]
    assert result.ambiguities == []
    assert result.intent_confidence == IntentConfidence.HIGH
    assert len(result.constraints) == 2
    assert parser.last_source == "live_llm"
    assert parser.last_error is None
    assert len(mock_provider.calls) == 1


# =============================================================================
# Test 2 — Malformed LLM Response
# Verify the parser handles invalid structured output safely.
# =============================================================================
def test_malformed_llm_response_handling():
    # 2a. GeminiProvider raises LLMResponseValidationError on malformed JSON
    provider = GeminiProvider(api_key="test_key", client=MagicMock())
    malformed_responses = [
        "not json at all",
        "{malformed_json: 123",
        "```json\n{unclosed_brace: 'val'\n```",
        "['array_instead_of_object']",
    ]

    for bad_text in malformed_responses:
        with pytest.raises(LLMResponseValidationError):
            provider._parse_structured_json(bad_text)

    # 2b. IntentParser with fallback_on_error=False re-raises LLMResponseValidationError
    mock_bad_provider = MockLLMProvider(
        side_effect=LLMResponseValidationError("Malformed response from model")
    )
    strict_parser = IntentParser(provider=mock_bad_provider, fallback_on_error=False)
    with pytest.raises(LLMResponseValidationError):
        strict_parser.parse("Move my meeting")

    # 2c. IntentParser with fallback_on_error=True handles error safely via observable fallback
    fallback_parser = IntentParser(provider=mock_bad_provider, fallback_on_error=True)
    fallback_intent = fallback_parser.parse("Move 3 PM meeting to 4 PM")
    assert isinstance(fallback_intent, StructuredIntent)
    assert fallback_parser.last_source == "offline_fallback"
    assert "Malformed response from model" in fallback_parser.last_error


# =============================================================================
# Test 3 — Missing Required Fields
# Verify invalid intent does not proceed as if it were valid.
# =============================================================================
def test_missing_required_fields_handling():
    # 3a. Incomplete payload missing required 'goal' or 'scope'
    provider = GeminiProvider(api_key="test_key", client=MagicMock())
    missing_fields_json = '{"entities": ["3 pm"], "ambiguities": []}'

    with pytest.raises(LLMResponseValidationError) as exc_info:
        provider._parse_structured_json(missing_fields_json)
    assert "validation failed" in str(exc_info.value).lower()

    # 3b. Verify invalid/empty intent cannot proceed to execution
    invalid_intent = StructuredIntent(
        goal="",
        scope="general",
        entities=[],
        constraints=[],
        ambiguities=["Missing target entities and unstated goal"],
        intent_confidence=IntentConfidence.LOW,
    )

    mcp = CareMCPServer()
    planner = DryRunPlanner(mcp_server=mcp)
    policy = PolicyEngine(mcp_server=mcp)

    candidate_plan = planner.generate_candidate_plan(invalid_intent)
    assert len(candidate_plan.actions) == 0, "No actions should be planned for empty/invalid intent"

    decision = policy.evaluate(candidate_plan)
    assert decision.outcome == PolicyOutcome.CLARIFY
    assert "Unresolved ambiguities detected" in decision.reason or "No concrete actions" in decision.reason


# =============================================================================
# Test 4 — Ambiguous Intent
# Verify ambiguity reaches the existing clarification path.
# =============================================================================
def test_ambiguous_intent_reaches_clarify():
    # Scenario: "Clear my afternoon so I can finish the proposal"
    # LLM accurately identifies unstated assumptions around external attendees
    ambiguous_intent = StructuredIntent(
        goal="Clear afternoon calendar",
        scope="calendar",
        entities=["afternoon"],
        constraints=[
            IntentConstraint(
                type=ConstraintType.PRESERVE_ITEMS,
                params={"property": "external_attendee"},
                source="inferred",
            ),
            IntentConstraint(
                type=ConstraintType.NO_DESTRUCTIVE_ACTIONS,
                params={},
                source="policy",
            ),
        ],
        ambiguities=["unspecified_treatment_of_external_attendees"],
        intent_confidence=IntentConfidence.MEDIUM,
    )

    mock_provider = MockLLMProvider(response=ambiguous_intent)
    parser = IntentParser(provider=mock_provider)
    intent = parser.parse("Clear my afternoon so I can finish the proposal.")

    assert len(intent.ambiguities) > 0
    assert intent.intent_confidence == IntentConfidence.MEDIUM

    # Pass through CARE pipeline
    mcp = CareMCPServer()
    planner = DryRunPlanner(mcp_server=mcp)
    policy = PolicyEngine(mcp_server=mcp)

    plan = planner.generate_candidate_plan(intent)
    decision = policy.evaluate(plan)

    # Must deterministically evaluate to CLARIFY, never AUTO_APPROVE
    assert decision.outcome == PolicyOutcome.CLARIFY
    assert "Unresolved ambiguities detected" in decision.reason


# =============================================================================
# Test 5 — LLM Unavailable / Offline Fallback Behavior
# =============================================================================
def test_llm_unavailable_and_offline_fallback():
    # 5a. Explicit offline mode
    offline_parser = IntentParser(mode="offline", use_cache=False)
    assert not offline_parser.is_live
    reschedule_intent = offline_parser.parse("Move 3 PM meeting to 4 PM")
    assert reschedule_intent.goal.lower() == "move meeting from 3 pm to 4 pm"
    assert offline_parser.last_source == "deterministic_rule"

    # 5b. Live provider network failure with observable fallback enabled
    failing_provider = MockLLMProvider(
        side_effect=LLMProviderUnavailableError("Gemini connection timed out")
    )
    fallback_parser = IntentParser(provider=failing_provider, fallback_on_error=True)

    intent = fallback_parser.parse("Move 3 PM meeting to 4 PM")
    assert isinstance(intent, StructuredIntent)
    assert fallback_parser.last_source == "offline_fallback"
    assert "Gemini connection timed out" in fallback_parser.last_error

    # 5c. Live provider network failure without fallback raises error (not silent)
    strict_parser = IntentParser(provider=failing_provider, fallback_on_error=False)
    with pytest.raises(LLMProviderUnavailableError) as exc_info:
        strict_parser.parse("Move 3 PM meeting to 4 PM")
    assert "Gemini connection timed out" in str(exc_info.value)


# =============================================================================
# Test 6 — LLM Does NOT Bypass Policy
# High LLM confidence must NOT override deterministic safety gates.
# =============================================================================
def test_llm_cannot_bypass_deterministic_policy():
    mcp = CareMCPServer()
    planner = DryRunPlanner(mcp_server=mcp)
    policy = PolicyEngine(mcp_server=mcp)

    # Case 6a: Escalated Ticket with HIGH LLM confidence
    high_conf_escalated_ticket = StructuredIntent(
        goal="Close ticket tkt_402",
        scope="tickets",
        entities=["tkt_402"],
        constraints=[],
        ambiguities=[],
        intent_confidence=IntentConfidence.HIGH,  # High LLM confidence!
    )
    plan_ticket = planner.generate_candidate_plan(high_conf_escalated_ticket)
    decision_ticket = policy.evaluate(plan_ticket)

    # Escalated ticket MUST require CONFIRM, ignoring HIGH confidence
    assert decision_ticket.outcome == PolicyOutcome.CONFIRM
    assert "active escalation" in decision_ticket.reason

    # Case 6b: READ_ONLY user role with HIGH LLM confidence
    high_conf_meeting_move = StructuredIntent(
        goal="Move meeting from 3 pm to 4 pm",
        scope="calendar",
        entities=["3 pm", "4 pm", "meeting"],
        constraints=[],
        ambiguities=[],
        intent_confidence=IntentConfidence.HIGH,
    )
    plan_readonly = planner.generate_candidate_plan(
        high_conf_meeting_move,
        actor="intern_readonly",
        user_role="READ_ONLY",
    )
    decision_readonly = policy.evaluate(plan_readonly)

    # READ_ONLY role MUST be BLOCKED, ignoring HIGH confidence
    assert decision_readonly.outcome == PolicyOutcome.BLOCK
    assert "unauthorized" in decision_readonly.reason.lower()


# =============================================================================
# Test 7 — LLM Cannot Directly Execute Tools
# The parser only returns StructuredIntent and has no tool execution boundary.
# =============================================================================
def test_llm_cannot_directly_execute_tools():
    # 7a. IntentParser & Provider have NO tool references or execution methods
    provider = GeminiProvider(api_key="test_key", client=MagicMock())
    parser = IntentParser(provider=provider)

    assert not hasattr(parser, "execute")
    assert not hasattr(parser, "dispatch_action")
    assert not hasattr(parser, "call_tool")
    assert not hasattr(provider, "execute")
    assert not hasattr(provider, "mcp_server")

    # 7b. Verify that calling parse() causes ZERO mutations to state stores
    mcp = CareMCPServer()
    initial_calendar_events = dict(mcp.calendar_store.events)
    initial_tickets = dict(mcp.tickets_store.tickets)

    mock_intent = StructuredIntent(
        goal="Cancel all meetings and close all tickets",
        scope="calendar",
        entities=["afternoon"],
        constraints=[],
        ambiguities=[],
        intent_confidence=IntentConfidence.HIGH,
    )
    mock_provider = MockLLMProvider(response=mock_intent)
    test_parser = IntentParser(provider=mock_provider)

    output = test_parser.parse("Cancel all meetings immediately")
    assert isinstance(output, StructuredIntent)

    # State stores must remain 100% untouched
    assert mcp.calendar_store.events == initial_calendar_events
    assert mcp.tickets_store.tickets == initial_tickets
