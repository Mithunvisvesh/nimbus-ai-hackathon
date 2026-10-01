import pytest
from src.schemas.intent import ConstraintType, IntentConfidence, IntentConstraint, StructuredIntent
from src.schemas.plan import CandidatePlan, PlannedAction, PlanStatus, RiskLevel
from src.schemas.tool_metadata import ToolMetadata
from src.verifier.invariant_checker import InvariantChecker


def make_test_intent(constraints=None):
    return StructuredIntent(
        goal="Reschedule team sync",
        scope="calendar",
        entities=["Weekly Sync"],
        constraints=constraints or [],
        ambiguities=[],
        intent_confidence=IntentConfidence.HIGH,
    )


def test_expected_state_equals_actual_state_success():
    """Verify that matching actual after_state passes."""
    action = PlannedAction(
        action_id="act_001",
        resource_type="calendar",
        resource_id="evt_sync",
        operation="update_time",
        parameters={
            "start_time": "2026-10-02T16:00:00Z",
            "end_time": "2026-10-02T16:30:00Z",
        },
        before_state={"start_time": "2026-10-02T15:00:00Z"},
        constraints=[
            IntentConstraint(
                type=ConstraintType.EXPECTED_STATE_EQUALS_ACTUAL_STATE,
                source="policy",
                params={},
            )
        ],
    )

    actual_state = {
        "id": "evt_sync",
        "start_time": "2026-10-02T16:00:00Z",
        "end_time": "2026-10-02T16:30:00Z",
        "status": "confirmed",
    }

    result = InvariantChecker.verify_action(action, actual_state)
    assert result.passed is True
    assert len(result.violations) == 0
    assert result.checked_count == 1


def test_expected_state_equals_actual_state_mismatch():
    """Verify that mismatched actual state triggers violation."""
    action = PlannedAction(
        action_id="act_001",
        resource_type="calendar",
        resource_id="evt_sync",
        operation="update_time",
        parameters={"start_time": "2026-10-02T16:00:00Z"},
        constraints=[
            IntentConstraint(
                type=ConstraintType.EXPECTED_STATE_EQUALS_ACTUAL_STATE,
                source="policy",
                params={},
            )
        ],
    )

    actual_state = {
        "id": "evt_sync",
        "start_time": "2026-10-02T17:00:00Z",  # Failed to set 16:00
    }

    result = InvariantChecker.verify_action(action, actual_state)
    assert result.passed is False
    assert len(result.violations) == 1
    assert result.violations[0].constraint_type == ConstraintType.EXPECTED_STATE_EQUALS_ACTUAL_STATE
    assert "start_time" in result.violations[0].message


def test_preserve_items_external_attendee_success():
    """Verify that preserving an external attendee passes if retained."""
    action = PlannedAction(
        action_id="act_002",
        resource_type="calendar",
        resource_id="evt_vip",
        operation="update_time",
        parameters={"start_time": "2026-10-02T16:00:00Z"},
        before_state={
            "attendees": [
                {"name": "Alice", "email": "alice@company.com", "is_external": False},
                {"name": "VIP Client", "email": "vip@acme.com", "is_external": True},
            ]
        },
        constraints=[
            IntentConstraint(
                type=ConstraintType.PRESERVE_ITEMS,
                source="explicit",
                params={"property": "external_attendee"},
            )
        ],
    )

    actual_state = {
        "attendees": [
            {"name": "Alice", "email": "alice@company.com", "is_external": False},
            {"name": "VIP Client", "email": "vip@acme.com", "is_external": True},
        ]
    }

    result = InvariantChecker.verify_action(action, actual_state)
    assert result.passed is True
    assert len(result.violations) == 0


def test_preserve_items_external_attendee_violation():
    """Verify that removing an external attendee triggers a violation."""
    action = PlannedAction(
        action_id="act_002",
        resource_type="calendar",
        resource_id="evt_vip",
        operation="update_time",
        parameters={"start_time": "2026-10-02T16:00:00Z"},
        before_state={
            "attendees": [
                {"name": "Alice", "email": "alice@company.com", "is_external": False},
                {"name": "VIP Client", "email": "vip@acme.com", "is_external": True},
            ]
        },
        constraints=[
            IntentConstraint(
                type=ConstraintType.PRESERVE_ITEMS,
                source="explicit",
                params={"property": "external_attendee"},
            )
        ],
    )

    # Actual state accidentally dropped VIP Client
    actual_state = {
        "attendees": [
            {"name": "Alice", "email": "alice@company.com", "is_external": False}
        ]
    }

    result = InvariantChecker.verify_action(action, actual_state)
    assert result.passed is False
    assert len(result.violations) == 1
    assert result.violations[0].constraint_type == ConstraintType.PRESERVE_ITEMS
    assert "vip@acme.com" in result.violations[0].message


def test_max_affected_plan_level_violation():
    """Verify that modifying more items than max_affected triggers violation."""
    plan_constraint = IntentConstraint(
        type=ConstraintType.MAX_AFFECTED,
        source="policy",
        params={"max_count": 2},
    )

    plan = CandidatePlan(
        plan_id="plan_test_01",
        intent=make_test_intent(constraints=[plan_constraint]),
        actor="user_mithun",
        user_role="STANDARD_USER",
        actions=[
            PlannedAction(action_id="a1", resource_type="calendar", resource_id="e1", operation="update"),
            PlannedAction(action_id="a2", resource_type="calendar", resource_id="e2", operation="update"),
            PlannedAction(action_id="a3", resource_type="calendar", resource_id="e3", operation="update"),
        ],
    )

    post_states = {"e1": {}, "e2": {}, "e3": {}}
    result = InvariantChecker.verify_plan(plan, post_states)

    assert result.passed is False
    assert any(v.constraint_type == ConstraintType.MAX_AFFECTED for v in result.violations)
    assert "exceeding allowed max_affected limit of 2" in result.violations[0].message


def test_no_destructive_actions_violation():
    """Verify that destructive operation triggers violation when forbidden."""
    action = PlannedAction(
        action_id="act_del",
        resource_type="calendar",
        resource_id="evt_01",
        operation="delete_event",
        parameters={"event_id": "evt_01"},
        constraints=[
            IntentConstraint(
                type=ConstraintType.NO_DESTRUCTIVE_ACTIONS,
                source="policy",
                params={},
            )
        ],
    )

    tool_meta = ToolMetadata(
        operation="delete_event",
        read_only=False,
        destructive=True,
        reversible=False,
        external_effect=False,
        affects_external_party=False,
        compensation_supported=False,
    )

    result = InvariantChecker.verify_action(action, {}, tool_metadata=tool_meta)
    assert result.passed is False
    assert len(result.violations) == 1
    assert result.violations[0].constraint_type == ConstraintType.NO_DESTRUCTIVE_ACTIONS


def test_verify_plan_end_to_end_success():
    """Verify an end-to-end plan passing all invariant checks."""
    plan = CandidatePlan(
        plan_id="plan_happy_path",
        intent=make_test_intent(
            constraints=[
                IntentConstraint(
                    type=ConstraintType.MAX_AFFECTED,
                    source="policy",
                    params={"max_count": 5},
                ),
                IntentConstraint(
                    type=ConstraintType.NO_DESTRUCTIVE_ACTIONS,
                    source="policy",
                    params={},
                ),
            ]
        ),
        actor="user_mithun",
        user_role="STANDARD_USER",
        actions=[
            PlannedAction(
                action_id="act_101",
                resource_type="tickets",
                resource_id="tkt_402",
                operation="update_status",
                parameters={"new_status": "in_progress"},
                constraints=[
                    IntentConstraint(
                        type=ConstraintType.EXPECTED_STATE_EQUALS_ACTUAL_STATE,
                        source="policy",
                        params={"expected": {"status": "in_progress"}},
                    )
                ],
            )
        ],
    )

    post_states = {
        "tkt_402": {
            "id": "tkt_402",
            "status": "in_progress",
            "title": "Payment gateway timeout",
        }
    }

    tool_lookup = {
        "update_status": ToolMetadata(
            operation="update_status",
            read_only=False,
            destructive=False,
            reversible=True,
            external_effect=False,
            affects_external_party=False,
            compensation_supported=True,
        )
    }

    result = InvariantChecker.verify_plan(plan, post_states, tool_lookup)
    assert result.passed is True
    assert len(result.violations) == 0
    assert result.checked_count == 3  # max_affected + no_destructive + expected_state
