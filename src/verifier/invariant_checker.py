"""Post-execution Invariant Checker for CARE.

Evaluates post-execution resource states against deterministic invariants
defined in the frozen interface contracts (ConstraintType):
  1. expected_state_equals_actual_state
  2. preserve_items
  3. max_affected
  4. no_destructive_actions

Distinguishes technical execution success (e.g. API HTTP 200 OK) from
intent-level correctness (state satisfies required invariants).
"""

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from src.schemas.intent import ConstraintType, IntentConstraint
from src.schemas.plan import CandidatePlan, PlannedAction
from src.schemas.tool_metadata import ToolMetadata


class InvariantViolation(BaseModel):
    """Detailed record of an invariant verification failure."""
    constraint_type: ConstraintType
    action_id: Optional[str] = None
    resource_id: Optional[str] = None
    message: str
    expected: Optional[Any] = None
    actual: Optional[Any] = None


class VerificationResult(BaseModel):
    """Overall outcome of post-execution invariant verification."""
    passed: bool
    checked_count: int = 0
    violations: List[InvariantViolation] = Field(default_factory=list)
    summary: str = ""

    def add_violation(self, violation: InvariantViolation) -> None:
        self.violations.append(violation)
        self.passed = False


class InvariantChecker:
    """Deterministic post-execution invariant verifier."""

    @classmethod
    def verify_action(
        cls,
        action: PlannedAction,
        actual_after_state: Dict[str, Any],
        tool_metadata: Optional[ToolMetadata] = None,
    ) -> VerificationResult:
        """Verify invariants for a single planned action against its observed post-execution state.

        Args:
            action: The PlannedAction containing parameters, before_state, and constraints.
            actual_after_state: The real live state of the resource after tool execution.
            tool_metadata: Optional trusted tool metadata describing operation characteristics.

        Returns:
            VerificationResult indicating pass/fail with structured violation details.
        """
        result = VerificationResult(passed=True, checked_count=0)

        # Action-specific constraints
        for constraint in action.constraints:
            cls._check_constraint_for_action(
                constraint=constraint,
                action=action,
                actual_after_state=actual_after_state,
                tool_metadata=tool_metadata,
                result=result,
            )

        if result.passed:
            result.summary = f"All {result.checked_count} invariant checks passed for action {action.action_id}."
        else:
            result.summary = (
                f"Action {action.action_id} failed {len(result.violations)} "
                f"of {result.checked_count} invariant checks."
            )

        return result

    @classmethod
    def verify_plan(
        cls,
        plan: CandidatePlan,
        post_execution_states: Dict[str, Dict[str, Any]],
        tool_metadata_lookup: Optional[Dict[str, ToolMetadata]] = None,
    ) -> VerificationResult:
        """Verify invariants across an entire plan, including intent-level global constraints.

        Args:
            plan: The CandidatePlan or ApprovedPlan that was executed.
            post_execution_states: Map of resource_id -> actual observed state dict.
            tool_metadata_lookup: Optional map of operation_name -> ToolMetadata.

        Returns:
            VerificationResult for the entire plan execution.
        """
        result = VerificationResult(passed=True, checked_count=0)

        # 1. Global / Plan-level constraints defined in plan.intent.constraints
        for constraint in plan.intent.constraints:
            cls._check_plan_level_constraint(
                constraint=constraint,
                plan=plan,
                post_execution_states=post_execution_states,
                tool_metadata_lookup=tool_metadata_lookup,
                result=result,
            )

        # 2. Per-action constraints
        for action in plan.actions:
            actual_state = post_execution_states.get(action.resource_id, {})
            metadata = (
                tool_metadata_lookup.get(action.operation)
                if tool_metadata_lookup
                else None
            )

            # Run action verification
            action_res = cls.verify_action(
                action=action,
                actual_after_state=actual_state,
                tool_metadata=metadata,
            )

            result.checked_count += action_res.checked_count
            for violation in action_res.violations:
                result.add_violation(violation)

        if result.passed:
            result.summary = f"All {result.checked_count} plan invariant checks passed successfully."
        else:
            result.summary = (
                f"Plan {plan.plan_id} failed {len(result.violations)} "
                f"of {result.checked_count} invariant checks."
            )

        return result

    # -------------------------------------------------------------------------
    # Internal Constraint Checkers
    # -------------------------------------------------------------------------

    @classmethod
    def _check_constraint_for_action(
        cls,
        constraint: IntentConstraint,
        action: PlannedAction,
        actual_after_state: Dict[str, Any],
        tool_metadata: Optional[ToolMetadata],
        result: VerificationResult,
    ) -> None:
        """Evaluate a single constraint for a specific action."""
        result.checked_count += 1
        c_type = constraint.type
        params = constraint.params

        if c_type == ConstraintType.EXPECTED_STATE_EQUALS_ACTUAL_STATE:
            cls._verify_expected_state(action, actual_after_state, params, result)

        elif c_type == ConstraintType.PRESERVE_ITEMS:
            cls._verify_preserve_items(action, actual_after_state, params, result)

        elif c_type == ConstraintType.MAX_AFFECTED:
            cls._verify_max_affected_action(action, actual_after_state, params, result)

        elif c_type == ConstraintType.NO_DESTRUCTIVE_ACTIONS:
            cls._verify_no_destructive_action(action, tool_metadata, result)

    @classmethod
    def _check_plan_level_constraint(
        cls,
        constraint: IntentConstraint,
        plan: CandidatePlan,
        post_execution_states: Dict[str, Dict[str, Any]],
        tool_metadata_lookup: Optional[Dict[str, ToolMetadata]],
        result: VerificationResult,
    ) -> None:
        """Evaluate a global constraint across all plan actions."""
        result.checked_count += 1
        c_type = constraint.type
        params = constraint.params

        if c_type == ConstraintType.MAX_AFFECTED:
            max_count = params.get("max_count", params.get("threshold", 100))
            actual_affected = len(plan.actions)
            if actual_affected > max_count:
                result.add_violation(
                    InvariantViolation(
                        constraint_type=ConstraintType.MAX_AFFECTED,
                        message=(
                            f"Plan affected {actual_affected} actions, exceeding "
                            f"allowed max_affected limit of {max_count}."
                        ),
                        expected=max_count,
                        actual=actual_affected,
                    )
                )

        elif c_type == ConstraintType.NO_DESTRUCTIVE_ACTIONS:
            for action in plan.actions:
                meta = (
                    tool_metadata_lookup.get(action.operation)
                    if tool_metadata_lookup
                    else None
                )
                if (meta and meta.destructive) or cls._is_destructive_op(action.operation):
                    result.add_violation(
                        InvariantViolation(
                            constraint_type=ConstraintType.NO_DESTRUCTIVE_ACTIONS,
                            action_id=action.action_id,
                            resource_id=action.resource_id,
                            message=(
                                f"Destructive operation '{action.operation}' detected in action "
                                f"'{action.action_id}', violating no_destructive_actions constraint."
                            ),
                            expected=False,
                            actual=True,
                        )
                    )

        elif c_type == ConstraintType.PRESERVE_ITEMS:
            # Check if any preserved resource ID or property was modified across plan
            preserve_ids = set(params.get("preserve_ids", []))
            for action in plan.actions:
                if action.resource_id in preserve_ids:
                    result.add_violation(
                        InvariantViolation(
                            constraint_type=ConstraintType.PRESERVE_ITEMS,
                            action_id=action.action_id,
                            resource_id=action.resource_id,
                            message=(
                                f"Resource '{action.resource_id}' was modified by action "
                                f"'{action.action_id}' but was explicitly protected in preserve_ids."
                            ),
                            expected=f"Preserve {action.resource_id}",
                            actual="Modified",
                        )
                    )

    # -------------------------------------------------------------------------
    # Helper Invariant Implementations
    # -------------------------------------------------------------------------

    @classmethod
    def _verify_expected_state(
        cls,
        action: PlannedAction,
        actual_after_state: Dict[str, Any],
        params: Dict[str, Any],
        result: VerificationResult,
    ) -> None:
        """Verify that actual resource state matches expected state after execution."""
        expected_dict = params.get("expected")
        if not expected_dict:
            # Check if a single property was specified (e.g. property="start_time")
            prop = params.get("property")
            if prop and prop in action.parameters:
                expected_dict = {prop: action.parameters[prop]}
            else:
                expected_dict = {
                    k: v for k, v in action.parameters.items()
                    if k not in ("plan_id", "action_hash", "event_id", "ticket_id")
                }

        mismatches = []
        for key, expected_val in expected_dict.items():
            actual_val = actual_after_state.get(key)
            if actual_val != expected_val:
                mismatches.append(
                    f"Field '{key}': expected {expected_val!r}, found {actual_val!r}"
                )

        if mismatches:
            result.add_violation(
                InvariantViolation(
                    constraint_type=ConstraintType.EXPECTED_STATE_EQUALS_ACTUAL_STATE,
                    action_id=action.action_id,
                    resource_id=action.resource_id,
                    message="State mismatch: " + "; ".join(mismatches),
                    expected=expected_dict,
                    actual={k: actual_after_state.get(k) for k in expected_dict},
                )
            )

    @classmethod
    def _verify_preserve_items(
        cls,
        action: PlannedAction,
        actual_after_state: Dict[str, Any],
        params: Dict[str, Any],
        result: VerificationResult,
    ) -> None:
        """Verify that preserved properties or attendees were not deleted or modified."""
        prop = params.get("property")

        # Specific check for external attendees preservation
        if prop == "external_attendee":
            before_attendees = action.before_state.get("attendees", [])
            after_attendees = actual_after_state.get("attendees", [])

            before_external = [
                a for a in before_attendees
                if isinstance(a, dict) and a.get("is_external") is True
            ]
            after_external_emails = {
                a.get("email") for a in after_attendees
                if isinstance(a, dict) and a.get("is_external") is True
            }

            for ext in before_external:
                ext_email = ext.get("email")
                if ext_email not in after_external_emails:
                    result.add_violation(
                        InvariantViolation(
                            constraint_type=ConstraintType.PRESERVE_ITEMS,
                            action_id=action.action_id,
                            resource_id=action.resource_id,
                            message=(
                                f"External attendee '{ext.get('name')} ({ext_email})' was removed "
                                f"from resource '{action.resource_id}'."
                            ),
                            expected=f"Retain external attendee {ext_email}",
                            actual="Removed",
                        )
                    )

        # Generic field/items preservation (supports preserve_fields, items, or property)
        preserved_fields = params.get("preserve_fields") or params.get("items") or []
        if isinstance(preserved_fields, str):
            preserved_fields = [preserved_fields]

        if prop and prop != "external_attendee" and prop not in preserved_fields:
            preserved_fields.append(prop)

        for field in preserved_fields:
            if field in action.before_state and field in actual_after_state:
                before_val = action.before_state.get(field)
                after_val = actual_after_state.get(field)
                if before_val != after_val:
                    result.add_violation(
                        InvariantViolation(
                            constraint_type=ConstraintType.PRESERVE_ITEMS,
                            action_id=action.action_id,
                            resource_id=action.resource_id,
                            message=(
                                f"Preserved field '{field}' changed from {before_val!r} to {after_val!r}."
                            ),
                            expected=before_val,
                            actual=after_val,
                        )
                    )

    @classmethod
    def _verify_max_affected_action(
        cls,
        action: PlannedAction,
        actual_after_state: Dict[str, Any],
        params: Dict[str, Any],
        result: VerificationResult,
    ) -> None:
        """Check action-level affected item limits if specified."""
        max_items = params.get("max_items")
        if max_items is not None:
            # Check list fields like attendees or tags
            affected_items = len(action.parameters.get("attendees", []))
            if affected_items > max_items:
                result.add_violation(
                    InvariantViolation(
                        constraint_type=ConstraintType.MAX_AFFECTED,
                        action_id=action.action_id,
                        resource_id=action.resource_id,
                        message=(
                            f"Action '{action.action_id}' affected {affected_items} items, "
                            f"exceeding max_items limit of {max_items}."
                        ),
                        expected=max_items,
                        actual=affected_items,
                    )
                )

    @classmethod
    def _verify_no_destructive_action(
        cls,
        action: PlannedAction,
        tool_metadata: Optional[ToolMetadata],
        result: VerificationResult,
    ) -> None:
        """Verify that action does not execute destructive operations."""
        is_dest = (
            tool_metadata.destructive
            if tool_metadata is not None
            else cls._is_destructive_op(action.operation)
        )
        if is_dest:
            result.add_violation(
                InvariantViolation(
                    constraint_type=ConstraintType.NO_DESTRUCTIVE_ACTIONS,
                    action_id=action.action_id,
                    resource_id=action.resource_id,
                    message=(
                        f"Action '{action.action_id}' executed destructive operation '{action.operation}' "
                        f"which violates no_destructive_actions invariant."
                    ),
                    expected=False,
                    actual=True,
                )
            )

    @staticmethod
    def _is_destructive_op(operation: str) -> bool:
        """Heuristic check for destructive operation names."""
        destructive_keywords = {"delete", "remove", "drop", "purge", "destroy", "truncate", "cancel"}
        op_lower = operation.lower()
        return any(kw in op_lower for kw in destructive_keywords)
