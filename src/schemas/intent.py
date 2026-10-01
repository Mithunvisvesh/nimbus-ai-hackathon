from enum import Enum
from typing import Any, Dict, List
from pydantic import BaseModel, Field

class IntentConfidence(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

class ConstraintType(str, Enum):
    PRESERVE_ITEMS = "preserve_items"
    MAX_AFFECTED = "max_affected"
    NO_DESTRUCTIVE_ACTIONS = "no_destructive_actions"
    EXPECTED_STATE_EQUALS_ACTUAL_STATE = "expected_state_equals_actual_state"

class IntentConstraint(BaseModel):
    type: ConstraintType
    params: Dict[str, Any] = Field(default_factory=dict)
    source: str = Field(description="explicit | inferred | policy")

class StructuredIntent(BaseModel):
    goal: str = Field(description="Normalized summary of what the user wants to achieve")
    scope: str = Field(description="Domain boundary (e.g., 'calendar', 'tickets', 'general')")
    entities: List[str] = Field(default_factory=list, description="Extracted targets, e.g. meeting names, dates")
    constraints: List[IntentConstraint] = Field(default_factory=list, description="Explicit or inferred constraint rules")
    ambiguities: List[str] = Field(default_factory=list, description="Unresolved assumptions or vague references")
    intent_confidence: IntentConfidence = Field(
        default=IntentConfidence.MEDIUM,
        description="Qualitative estimate only. NEVER used as sole threshold for execution."
    )
