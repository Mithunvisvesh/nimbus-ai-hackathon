from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field
from src.schemas.intent import IntentConstraint, StructuredIntent

class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

class PolicyOutcome(str, Enum):
    AUTO_APPROVE = "auto_approve"
    CLARIFY = "clarify"
    CONFIRM = "confirm"
    BLOCK = "block"

class PlanStatus(str, Enum):
    PLANNED = "planned"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    APPROVED = "approved"
    BLOCKED = "blocked"
    TERMINATED = "terminated"
    EXECUTING = "executing"
    DONE = "done"
    FAILED = "failed"
    RECOVERING = "recovering"
    COMPENSATED = "compensated"
    DRIFT = "drift"

class CompensationAction(BaseModel):
    operation: str
    parameters: Dict[str, Any]

class PlannedAction(BaseModel):
    action_id: str
    resource_type: str = Field(description="calendar | ticket | file")
    resource_id: str
    operation: str
    parameters: Dict[str, Any] = Field(default_factory=dict)
    before_state: Dict[str, Any] = Field(default_factory=dict, description="Captured during dry-run read")
    risk_level: RiskLevel = Field(default=RiskLevel.LOW, description="Advisory candidate value; deterministic policy validates against trusted tool metadata")
    reversible: bool = Field(default=True, description="Advisory candidate value; authoritative value comes from trusted tool metadata")
    compensation_action: Optional[CompensationAction] = None
    constraints: List[IntentConstraint] = Field(default_factory=list)

class CandidatePlan(BaseModel):
    plan_id: str
    intent: StructuredIntent
    actor: str = Field(description="Requesting user identifier")
    user_role: str = Field(description="Authenticated/simulated request role: ADMIN | STANDARD_USER | READ_ONLY")
    actions: List[PlannedAction]
    action_hash: Optional[str] = None
    policy_outcome: Optional[PolicyOutcome] = None
    status: PlanStatus = PlanStatus.PLANNED
