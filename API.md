# API Specifications & MCP Tool Interfaces

## Project: CARE (Context-Aware Reasoning & Execution)
**Event:** NIMBUS Hackathon 2026  
**Document Version:** 1.0.0  
**Status:** Frozen Baseline  

---

## 1. Architectural Overview

CARE operates across two distinct interface boundaries:
1. **The External / UI Orchestrator REST API (FastAPI):** High-level endpoints coordinating parsing, dry run, policy gating, execution, and audit queries.
2. **The Model Context Protocol (MCP) Server:** Standardized tool server providing namespaced domain capabilities (`calendar.*`, `tickets.*`, `files.*`) with privilege enforcement.

```
 [User / Streamlit UI]
           │
           ▼ (HTTP JSON REST)
 ┌────────────────────────┐
 │ Orchestrator REST API  │
 └─────────┬──────────────┘
           │
           ▼ (Internal Orchestrator Calls)
 ┌────────────────────────┐
 │   Controlled Executor  │
 └─────────┬──────────────┘
           │
           ▼ (MCP Tool Protocol with plan_id & action_hash)
 ┌────────────────────────┐
 │    CARE MCP Server     │
 │ (calendar / tickets)   │
 └────────────────────────┘
```

---

## 2. Model Context Protocol (MCP) Tool Specification

State-changing tools in CARE enforce authorization guards: they can **only** be invoked if supplied with an authorized `plan_id` and matching `action_hash`. Agents are only provided with **read-only** discovery tools.

### 2.1 Calendar Tools (`calendar.*`)

#### Read-Only Tools

##### `calendar.list_events`
- **Description:** Retrieve events within a given time range.
- **Parameters:**
  ```json
  {
    "start_time": "2026-10-02T00:00:00Z",
    "end_time": "2026-10-02T23:59:59Z"
  }
  ```
- **Returns:** `List[CalendarEvent]`

##### `calendar.check_availability`
- **Description:** Check if a specific time slot is free of conflicts.
- **Parameters:**
  ```json
  {
    "start_time": "2026-10-02T17:00:00Z",
    "end_time": "2026-10-02T17:30:00Z"
  }
  ```
- **Returns:**
  ```json
  {
    "available": false,
    "conflicting_event": {
      "id": "evt_5pm_hold",
      "title": "Strategy Review"
    }
  }
  ```

#### State-Changing Tools (Gated)

##### `calendar.update_event`
- **Privilege Required:** `EXECUTOR` with valid `plan_id` & `action_hash`
- **Parameters:**
  ```json
  {
    "plan_id": "plan_982f1b8a-3e12",
    "action_hash": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "event_id": "evt_3pm_sync",
    "start_time": "2026-10-02T17:00:00Z",
    "end_time": "2026-10-02T17:30:00Z"
  }
  ```
- **Metadata Declared:** `reversible: true`, `destructive: false`, `external_effect: false`
- **Returns:** Updated `CalendarEvent` object.

##### `calendar.delete_event`
- **Privilege Required:** `EXECUTOR` with valid `plan_id` & `action_hash`
- **Parameters:** `plan_id`, `action_hash`, `event_id`
- **Metadata Declared:** `reversible: true` (soft delete with restore capability)

---

### 2.2 Tickets Tools (`tickets.*`)

#### Read-Only Tools

##### `tickets.get_ticket`
- **Parameters:** `ticket_id: str`
- **Returns:** `TicketItem` (priority, status, escalation tags, assigned user).

##### `tickets.list_tickets`
- **Parameters:** `status: Optional[str]`, `is_escalated: Optional[bool]`
- **Returns:** `List[TicketItem]`

#### State-Changing Tools (Gated)

##### `tickets.update_status`
- **Privilege Required:** `EXECUTOR` with valid `plan_id` & `action_hash`
- **Parameters:**
  ```json
  {
    "plan_id": "plan_982f1b8a-3e12",
    "action_hash": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "ticket_id": "tkt_402",
    "new_status": "closed",
    "resolution_notes": "Resolved payment timeout."
  }
  ```
- **Metadata Declared:** `reversible: true` (can be reopened), `destructive: false`, `external_effect: false`
- **Returns:** Updated `TicketItem` object.

---

## 3. Orchestrator Engine REST API

Base URL: `http://localhost:8000/api/v1`

### 3.1 Intent Parsing
- **Endpoint:** `POST /intent/parse`
- **Description:** Probabilistically extracts structured intent from natural language.
- **Request Body:**
  ```json
  {
    "prompt": "Move my 3 PM meeting to 5 PM",
    "user_id": "user_mithun"
  }
  ```
- **Response (200 OK):** `StructuredIntent` JSON schema.

---

### 3.2 Dry Run & Plan Generation
- **Endpoint:** `POST /plan/dry-run`
- **Description:** Runs read-only tool queries to resolve concrete targets and before-states.
- **Request Body:**
  ```json
  {
    "intent": { ... },
    "actor": "user_mithun",
    "user_role": "STANDARD_USER"
  }
  ```
- **Response (200 OK):**
  ```json
  {
    "plan_id": "plan_982f1b8a-3e12",
    "actions": [
      {
        "action_id": "act_001",
        "resource_type": "calendar",
        "resource_id": "evt_3pm_sync",
        "operation": "update_time",
        "parameters": {"start_time": "2026-10-02T17:00:00Z"},
        "before_state": {"start_time": "2026-10-02T15:00:00Z"},
        "risk_level": "low",
        "reversible": true
      }
    ],
    "action_hash": "e3b0c442...",
    "status": "planned"
  }
  ```

---

### 3.3 Policy Evaluation
- **Endpoint:** `POST /policy/evaluate`
- **Description:** Deterministically evaluates candidate plan against RBAC, ambiguity, and risk rules.
- **Request Body:** `CandidatePlan`
- **Response (200 OK):**
  ```json
  {
    "policy_outcome": "clarify",
    "reason": "Target slot 5:00 PM is already occupied by 'Strategy Review'.",
    "suggested_alternatives": ["2026-10-02T16:00:00Z", "2026-10-02T17:30:00Z"]
  }
  ```

---

### 3.4 Plan Execution
- **Endpoint:** `POST /executor/execute`
- **Description:** Verifies plan hash, checks pre-execution resource freshness, logs write-ahead journal, and invokes tools.
- **Request Body:**
  ```json
  {
    "plan_id": "plan_982f1b8a-3e12",
    "approved_action_hash": "e3b0c442..."
  }
  ```
- **Response (200 OK):**
  ```json
  {
    "plan_id": "plan_982f1b8a-3e12",
    "status": "done",
    "executed_actions": 1,
    "journal_records": ["rec_101"]
  }
  ```
- **Response (409 Conflict - Stale State):**
  ```json
  {
    "error": "STALE_RESOURCE_STATE",
    "resource_id": "evt_3pm_sync",
    "message": "Resource state changed between planning and execution. Re-planning required."
  }
  ```

---

### 3.5 Journal Audit Trail
- **Endpoint:** `GET /journal/{plan_id}`
- **Description:** Retrieves the complete append-only audit trail for a given plan execution.
- **Response (200 OK):** `List[JournalRecord]`

---

### 3.6 Demo Drift Simulation
- **Endpoint:** `POST /demo/simulate-drift`
- **Description:** Modifies a resource directly in mock storage behind the agent's back to demo drift detection.
- **Request Body:**
  ```json
  {
    "resource_id": "evt_3pm_sync",
    "simulated_modifications": {
      "title": "VIP Rescheduled Manually"
    }
  }
  ```
- **Response (200 OK):**
  ```json
  {
    "status": "drift_simulated",
    "resource_id": "evt_3pm_sync"
  }
  ```

---

## 4. Standard Error Codes & Envelope

```json
{
  "error_code": "POLICY_BLOCKED | STALE_STATE | ACTION_HASH_MISMATCH | DRIFT_DETECTED | EXECUTION_FAILED",
  "message": "Human readable explanation of the failure",
  "details": {},
  "timestamp": "2026-10-01T23:00:00Z"
}
```
