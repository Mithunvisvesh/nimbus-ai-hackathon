# Data Architecture & Schemas

## Project: CARE (Context-Aware Reasoning & Execution)
**Event:** NIMBUS Hackathon 2026  
**Document Version:** 1.0.0  
**Status:** Frozen Baseline Contracts  

---

## 1. Data Flow Architecture

CARE coordinates state through a durable, verifiable data pipeline. State-changing operations are controlled through approved action hashes, pre-execution freshness checks, and a durable action journal.

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
    TERMINATED = "terminated"
    EXECUTING = "executing"
    DONE = "done"
    FAILED = "failed"
    RECOVERING = "recovering"
    COMPENSATED = "compensated"
    DRIFT = "drift"

# Note on Clarification:
# When policy returns 'CLARIFY', the current candidate plan is rejected and persisted as TERMINATED.
# The orchestrator asks the user for clarification, and upon user response, an entirely
# NEW CandidatePlan with a fresh plan_id is generated. Clarification is not a persistent
# executing state for an existing plan.

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
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
```

> **Provenance rule:** `actor` and `user_role` are supplied by the authenticated/simulated request context, never inferred by the LLM. Security-relevant action metadata such as risk, reversibility, destructive behavior, external impact, and compensation support is authoritative only when supplied by the trusted control/tool layer.

---

### 2.4 Trusted Tool Metadata (`src/schemas/tool_metadata.py`)

Security-sensitive operation properties are deterministic metadata owned by the tool/control layer. LLM-proposed values are advisory and cannot independently authorize execution.

```python
from pydantic import BaseModel

class ToolMetadata(BaseModel):
    operation: str
    read_only: bool
    destructive: bool
    reversible: bool
    external_effect: bool
    affects_external_party: bool
    compensation_supported: bool
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

-- Table: approved_plans
CREATE TABLE IF NOT EXISTS approved_plans (
    plan_id TEXT PRIMARY KEY,
    actor TEXT NOT NULL,
    user_role TEXT NOT NULL,
    policy_outcome TEXT NOT NULL,
    action_hash TEXT NOT NULL,
    actions_json TEXT NOT NULL,              -- canonical JSON serialized
    status TEXT NOT NULL,                   -- awaiting_confirmation | approved | executing | done | failed | recovering | compensated | drift | terminated
    created_at TEXT NOT NULL,
    approved_at TEXT,
    executing_at TEXT,
    completed_at TEXT
);

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
    status TEXT NOT NULL,              -- planned | executing | unknown | done | failed | compensated | drift
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

-- Plan & audit indexes
CREATE INDEX IF NOT EXISTS idx_approved_plans_status ON approved_plans(status);
CREATE INDEX IF NOT EXISTS idx_journal_plan_id ON journal_records(plan_id);
CREATE INDEX IF NOT EXISTS idx_journal_resource ON journal_records(resource_type, resource_id);
CREATE INDEX IF NOT EXISTS idx_journal_status ON journal_records(status);
```
> *Note on Audit Preservation:* The action journal is a **durable action journal with application-level audit preservation**. Historical records are never deleted or overwritten; the current transaction record may be updated with execution outcomes such as `executing`, `done`, or `failed`. SQLite WAL mode provides database-level durability/concurrency, not cryptographic immutability.

---

### 4.1 Durable Approved Plan

`ApprovedPlan` is the persisted approved representation of a `CandidatePlan`; it does not require a separate action schema. The durable `approved_plans` record is the server-side authorization anchor used by the Controlled Executor. It stores the exact action list, `action_hash`, actor, role, policy outcome, and lifecycle status.

The executor must use this stored record rather than trusting a client-supplied hash or reconstructed plan as proof of authorization.

---

## 5. Hash-Based Plan Integrity & Action-List Mismatch Detection

The `action_hash` detects whether the concrete action list being executed matches the exact action list that was evaluated and approved by the policy engine. Authorization is bound server-side to the durable `ApprovedPlan` record associated with the `plan_id`, which binds `actor`, `user_role`, `policy_outcome`, `status`, and `actions`. The hash is not an authorization credential by itself.

To compute `action_hash`:
1. Extract list of `actions` from `CandidatePlan`.
2. Cleanse actions by discarding dynamic runtime fields (`status`, `result`, `error`).
3. Serialize to canonical JSON with sorted keys and compact separators:  
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

## 6. State Transition Lifecycles & Replay Protection

### Plan Lifecycle State Machine
```
[PLANNED]
   │
   ├── (Policy: CLARIFY) ─────────► [TERMINATED / RE-PLAN] ──► (User Input) ──► [NEW PLAN]
   ├── (Policy: CONFIRM) ─────────► [AWAITING_CONFIRMATION] ──► (Approved) ──┐
   ├── (Policy: BLOCK) ───────────► [BLOCKED] (Terminated)                   │
   └── (Policy: AUTO-APPROVE) ────► [APPROVED] ◄─────────────────────────────┘
                                       │
                                       ▼ (Single-Use Execution Gate)
                                  [EXECUTING]  ◄── State immediately locked
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

### Deterministic Single-Use Replay Protection Rule
A `plan_id` can be executed **exactly once**. The Controlled Executor strictly asserts:
1. `plan_id` exists in durable store.
2. Stored `plan.status == PlanStatus.APPROVED`.
3. `submitted_action_hash == stored_plan.action_hash`.
4. Requesting actor and role match the authorized plan record.
5. Immediately before each state-changing action, the live target resource matches that action's `before_state` (per-action freshness check).

The state transition is implemented as an atomic compare-and-set, conceptually:

```sql
UPDATE approved_plans
SET status = 'executing', executing_at = CURRENT_TIMESTAMP
WHERE plan_id = ? AND status = 'approved';
```

The executor must require exactly one affected row before proceeding.

Immediately upon satisfying these conditions, the executor atomically transitions `plan.status` from `APPROVED` to `EXECUTING`. The database update must succeed for exactly one row; concurrent or subsequent submissions fail with `PLAN_ALREADY_CONSUMED`. This provides application-level single-use plan replay protection.

### Confirmation Binding
For a `CONFIRM` outcome, the user's confirmation authorizes the exact persisted plan identified by `plan_id` and `action_hash`. The executor must not regenerate, reorder, or modify the action list after confirmation. Any change requires a new plan and new authorization.

### Unknown Tool Outcome / Reconciliation
If a state-changing tool may have been dispatched but the response is lost or ambiguous, the action is recorded as `UNKNOWN`, not `FAILED`. The recovery engine must reconcile the live external resource against the journaled `before_state` and expected `after_state` before deciding whether the action succeeded, failed, or requires recovery. An `UNKNOWN` action must never be blindly retried or compensated without reconciliation.
