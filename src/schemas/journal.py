from enum import Enum
from typing import Any, Dict, Optional
from pydantic import BaseModel, Field
from datetime import datetime, timezone

class ActionJournalStatus(str, Enum):
    PLANNED = "planned"
    EXECUTING = "executing"
    DONE = "done"
    FAILED = "failed"
    COMPENSATED = "compensated"
    DRIFT = "drift"
    UNKNOWN = "unknown"

class JournalRecord(BaseModel):
    plan_id: str
    action_id: str
    action_hash: str
    actor: str
    user_role: str
    resource_type: str
    resource_id: str
    operation: str
    before_state: Dict[str, Any]
    after_state: Optional[Dict[str, Any]] = None
    compensation_action: Optional[Dict[str, Any]] = None
    status: ActionJournalStatus
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
