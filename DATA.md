# Data Architecture & Schemas

## Project: CARE (Context-Aware Reasoning & Execution)
**Event:** NIMBUS Hackathon 2026  
**Document Version:** 1.0.0  
**Status:** Frozen Baseline Contracts  

---

## 1. Data Flow Architecture

CARE coordinates state through an append-only, verifiable data pipeline. State never mutates in-place without cryptographic integrity tracking and write-ahead journaling.

```
                  ┌──────────────────────┐
                  │ Natural Lang Prompt  │
                  └──────────┬───────────┘
                             │
                             ▼
                  ┌──────────────────────┐
                  │   StructuredIntent   │ (Pydantic / JSON Schema)
                  └──────────┬───────────┘
                             │
                             ▼
                  ┌──────────────────────┐
                  │    CandidatePlan     │ (Actions with before_state)
                  └──────────┬───────────┘
                             │
                             ▼
                  ┌──────────────────────┐
                  │     ApprovedPlan     │ (plan_id + canonical action_hash)
                  └──────────┬───────────┘
                             │
                ┌────────────┴────────────┐
                ▼                         ▼
   ┌─────────────────────────┐  ┌─────────────────────────┐
   │ SQLite Action Journal   │  │   Controlled Executor   │
   │ (Write-Ahead Logging)   │  │ (Live Tool Invocations) │
   └─────────────────────────┘  └─────────────────────────┘
```

---

## 2. Core System Data Contracts (Frozen Schemas)

### 2.1 Structured Intent (`src/schemas/intent.py`)

Represents the probabilistic LLM interpretation of the user's natural language request.

```python
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
```

---

### 2.2 Planned Action & Plan (`src/schemas/plan.py`)

Represents concrete operations generated during read-only dry run and gated by policy.

```python
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

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
    risk_level: RiskLevel = Field(default=RiskLevel.LOW)
    reversible: bool = Field(default=True)
    compensation_action: Optional[CompensationAction] = None
    constraints: List[IntentConstraint] = Field(default_factory=list)

class CandidatePlan(BaseModel):
    plan_id: str
    intent: StructuredIntent
    actor: str = Field(description="Requesting user identifier")
    user_role: str = Field(default="STANDARD_USER", description="ADMIN | STANDARD_USER | READ_ONLY")
    actions: List[PlannedAction]
    action_hash: Optional[str] = None
    policy_outcome: Optional[PolicyOutcome] = None
    status: PlanStatus = PlanStatus.PLANNED
```

---

### 2.3 Journal Record (`src/schemas/journal.py`)

Persistent write-ahead transaction log schema.

```python
from enum import Enum
from typing import Any, Dict, Optional
from pydantic import BaseModel, Field
from datetime import datetime

class ActionJournalStatus(str, Enum):
    PLANNED = "planned"
    EXECUTING = "executing"
    DONE = "done"
    FAILED = "failed"
    COMPENSATED = "compensated"
    DRIFT = "drift"

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
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
```

---

## 3. Mock Domain Schemas (In-Memory / Fixtures)

### 3.1 Calendar Domain (`src/mcp/domains/calendar_module.py`)
```json
{
  "id": "evt_3pm_sync",
  "title": "Weekly Team Sync",
  "start_time": "2026-10-02T15:00:00Z",
  "end_time": "2026-10-02T15:30:00Z",
  "attendees": [
    {"name": "Alice", "email": "alice@company.com", "is_external": false},
    {"name": "Client VP", "email": "vp@acme-corp.com", "is_external": true}
  ],
  "location": "Virtual / Meet",
  "status": "confirmed"
}
```

### 3.2 Tickets Domain (`src/mcp/domains/tickets_module.py`)
```json
{
  "id": "tkt_402",
  "title": "Payment gateway timeout for enterprise tenant",
  "priority": "P1",
  "status": "in_progress",
  "tags": ["escalation", "enterprise", "billing"],
  "assigned_to": "engineer_bob",
  "description": "API requests to stripe gateway failing with 504.",
  "is_escalated": true
}
```

---

## 4. SQLite Storage DDL (Write-Ahead Journal)

CARE utilizes an embedded SQLite database running with Write-Ahead Logging (`WAL` mode) for reliable persistence.

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- Table: journal_records
CREATE TABLE IF NOT EXISTS journal_records (
    record_id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    action_hash TEXT NOT NULL,
    actor TEXT NOT NULL,
    user_role TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    before_state TEXT NOT NULL,         -- JSON serialized
    after_state TEXT,                  -- JSON serialized (NULL until tool succeeds)
    compensation_action TEXT,          -- JSON serialized
    status TEXT NOT NULL,              -- planned | executing | done | failed | compensated | drift
    timestamp TEXT NOT NULL,           -- ISO-8601 UTC
    result TEXT,                       -- JSON serialized
    error TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Table: drift_incidents
CREATE TABLE IF NOT EXISTS drift_incidents (
    incident_id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    expected_after_state TEXT NOT NULL,
    observed_drift_state TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    remediation_status TEXT DEFAULT 'pending_human_review',
    notes TEXT
);

-- Performance & Audit Indexes
CREATE INDEX IF NOT EXISTS idx_journal_plan_id ON journal_records(plan_id);
CREATE INDEX IF NOT EXISTS idx_journal_resource ON journal_records(resource_type, resource_id);
CREATE INDEX IF NOT EXISTS idx_journal_status ON journal_records(status);
```

---

## 5. Canonical Hash Algorithm Specification

To guarantee tamper resistance:
1. Extract list of `actions` from `CandidatePlan`.
2. Cleanse actions by discarding dynamic runtime fields (`status`, `result`, `error`).
3. Serialize to JSON with sorted keys and compact separators:  
   `canonical_json = json.dumps(clean_actions, sort_keys=True, separators=(',', ':'))`
4. Compute SHA-256 digest:  
   `action_hash = hashlib.sha256(canonical_json.encode('utf-8')).hexdigest()`

```python
import json
import hashlib

def compute_action_hash(actions: list) -> str:
    cleaned = []
    for act in actions:
        data = act.copy() if isinstance(act, dict) else act.dict()
        data.pop("status", None)
        cleaned.append(data)
    canonical = json.dumps(cleaned, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()
```

---

## 6. State Transition Lifecycles

### Plan Lifecycle State Machine
```
[PLANNED]
   │
   ├── (Policy: CLARIFY) ─────────► [AWAITING_CONFIRMATION / RE-PLAN]
   ├── (Policy: CONFIRM) ─────────► [AWAITING_CONFIRMATION] ──► (Approved) ──┐
   ├── (Policy: BLOCK) ───────────► [BLOCKED] (Terminated)                   │
   └── (Policy: AUTO-APPROVE) ────► [APPROVED] ◄─────────────────────────────┘
                                       │
                                       ▼
                                  [EXECUTING]
                                       │
                         ┌─────────────┴─────────────┐
                         ▼                           ▼
                      [DONE]                      [FAILED]
                                                     │
                                                     ▼
                                               [RECOVERING]
                                                     │
                                       ┌─────────────┴─────────────┐
                                       ▼                           ▼
                                 [COMPENSATED]                  [DRIFT]
                                                      (Requires Human Review)
```
