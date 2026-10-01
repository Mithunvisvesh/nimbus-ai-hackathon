# API Specifications & MCP Tool Interfaces

## Project: CARE (Context-Aware Reasoning & Execution)
**Event:** NIMBUS Hackathon 2026  
**Document Version:** 1.0.0  
**Status:** Frozen Baseline  

---

## 1. Architectural Overview

CARE's intended interface boundaries are documented here. The current prototype implements the FastMCP server and its calendar/ticket namespaces; the FastAPI REST orchestrator shown below is a design specification and has no runtime entry point yet. A files namespace is also outside the current prototype scope.

```
 [User / Streamlit UI]
           │
           ▼ (HTTP JSON REST — design specification; not currently implemented)
 ┌────────────────────────┐
 │ Orchestrator REST API  │
 └─────────┬──────────────┘
           │
           ▼ (Internal Orchestrator Calls)
 ┌────────────────────────┐
 │   Controlled Executor  │
 └─────────┬──────────────┘
           │
           ▼ (MCP protocol; executor supplies its one-time dispatch capability)
 ┌────────────────────────┐
 │    CARE MCP Server     │
 │ (calendar / tickets)   │
 └────────────────────────┘
```

---

## 2. Model Context Protocol (MCP) Tool Specification

State-changing tools are exposed by the FastMCP server but cannot be authorized by caller-supplied plan identifiers alone. Before dispatch, the Controlled Executor validates approval, re-evaluates deterministic policy, checks the canonical action hash and resource freshness, atomically transitions the plan from `APPROVED` to `EXECUTING`, and writes `before_state` to the journal. It then sends the exact planned operation, resource, parameters, action ID, and a one-time dispatch capability to the MCP tool.

At the mutation boundary, the server checks that:

1. The plan exists, has an executable policy outcome, and is in `EXECUTING` (or `RECOVERING` for compensation).
2. The supplied hash matches both the persisted plan hash and its canonical action list.
3. The action ID, operation, resource, and parameters exactly match the persisted action (or its predefined compensation).
4. A matching write-ahead journal record exists in the required state.
5. The one-time executor or recovery capability matches its server-stored hash and has not been consumed.

After dispatch, the executor records the result, runs per-action and plan-level invariant checks, and invokes drift-checked compensation on failure where safe. Recovery reconciles `EXECUTING`/`UNKNOWN` journal records before compensating. Read-only discovery tools remain available without mutation authorization.

FastMCP server entry point: `python -m src.mcp.fastmcp_server` (stdio transport).

### 2.1 Calendar Tools (`calendar.*`)

> *Note on Demonstration Focus:* For clean, unambiguous reversibility and compensation, the demo focuses primarily on `calendar.update_event` and `tickets.update_status` / `tickets.reopen_ticket`.

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
- **Privilege Required:** The executor supplies the exact approved action and one-time dispatch capability. Caller-supplied `plan_id` and `action_hash` alone are insufficient.
- **Parameters:**
  ```json
  {
    "plan_id": "plan_982f1b8a-3e12",
    "action_hash": "<persisted approved action hash>",
    "action_id": "act_001",
    "dispatch_token": "<one-time executor capability>",
    "event_id": "evt_3pm_sync",
    "start_time": "2026-10-02T17:00:00Z",
    "end_time": "2026-10-02T17:30:00Z"
  }
  ```
- **Metadata Declared:** `reversible: true`, `destructive: false`, `external_effect: false`
- **Returns:** Updated `CalendarEvent` object.

`calendar.delete_event` is not implemented in the current prototype.

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
- **Privilege Required:** The executor supplies the exact approved action and one-time dispatch capability. Caller-supplied `plan_id` and `action_hash` alone are insufficient.
- **Parameters:**
  ```json
  {
    "plan_id": "plan_982f1b8a-3e12",
    "action_hash": "<persisted approved action hash>",
    "action_id": "act_001",
    "dispatch_token": "<one-time executor capability>",
    "ticket_id": "tkt_402",
    "new_status": "closed",
    "resolution_notes": "Resolved payment timeout."
  }
  ```
- **Metadata Declared:** `reversible: true` (can be reopened), `destructive: false`, `external_effect: false`
- **Returns:** Updated `TicketItem` object.

---

## 3. Orchestrator Engine REST API (Design Specification Only)

The following HTTP endpoints are planned interface documentation, not implemented routes. The current runnable mutation path is through the Controlled Executor and FastMCP server described above. The available parser uses cached responses and deterministic fallback patterns; real LLM provider integration remains teammate-owned.

Base URL: `http://localhost:8000/api/v1`

### 3.1 Intent Parsing
- **Endpoint:** `POST /intent/parse`
- **Description:** Target endpoint for structured intent extraction; the current parser uses cached responses and deterministic patterns.
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
        "operation": "calendar.update_event",
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
- **Response (409 Conflict - Single-Use Replay Rejection):**
  ```json
  {
    "error": "PLAN_ALREADY_CONSUMED",
    "plan_id": "plan_982f1b8a-3e12",
    "message": "This plan has already been executed or is currently executing. Replay is rejected."
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
  "error_code": "POLICY_BLOCKED | STALE_STATE | ACTION_HASH_MISMATCH | PLAN_ALREADY_CONSUMED | DRIFT_DETECTED | EXECUTION_FAILED",
  "message": "Human readable explanation of the failure",
  "details": {},
  "timestamp": "2026-10-01T23:00:00Z"
}
```
