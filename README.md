# NIMBUS 2026 — CARE: Context-Aware Reasoning & Execution

> **A hybrid control layer for AI agents that separates natural-language intent interpretation from action authorization and execution.**  
> Ambiguous, multi-step, or consequential requests are planned, gated, verified, and recovered without relying on hard-coded scenarios.

---

## 1. Executive Summary & Core Thesis

**Project in One Sentence:**  
CARE builds an enterprise-oriented control layer for autonomous AI agents that strictly decouples probabilistic natural-language interpretation from deterministic authorization and execution, ensuring agent actions are safely planned, policy-gated, integrity-checked, verified, and compensable.

### The Core Problem
Modern AI assistants frequently execute technical tool calls successfully while failing to satisfy the user's actual goal. Ambiguity, implicit constraints, and unstated assumptions create a critical gap between **technical execution success** and **intent-level correctness**:
- An agent told to *"clear my afternoon"* might blindly cancel a crucial meeting with an external VIP client.
- An agent instructed to *"reschedule a team sync"* might double-book attendees into an unfeasible slot without checking real resource state.
- Tool-level HTTP `200 OK` responses do not guarantee that the resulting system state matches user intent.
- Blind rollback mechanisms in multi-tenant environments risk overwriting legitimate modifications made after the agent's action.

### The CARE Solution: Hybrid Control Architecture
CARE enforces a clear division of responsibility:
- **The LLM Proposes:** Probabilistic extraction of intent, goals, constraints, and candidate dry-run action sequences.
- **The Control Layer Decides:** Deterministic evaluation of permissions, scope, risk, feasibility, and safety policies.
- **The Executor Acts:** Integrity-verified, freshness-checked execution against tools via a write-ahead journal, followed by post-execution verification and saga-style compensation.

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                                   THE CARE PIPELINE                                    │
└────────────────────────────────────────────────────────────────────────────────────────┘

  User Request (Natural Language)
                │
                ▼
      ┌──────────────────┐
      │  Intent Parser   │  ◄── Probabilistic LLM (Extracts goal, scope, constraints)
      └─────────┬────────┘
                │
                ▼
      ┌──────────────────┐
      │ Planner / Dry Run│  ◄── Read-only target resolution (Resolves concrete actions)
      └─────────┬────────┘
                │ Concrete Planned Actions
                ▼
   ┌─────────────────────────┐
   │  Policy & Risk Engine   │  ◄── Deterministic Rules (Permissions, risk, reversibility)
   └────────────┬────────────┘
                │
     ┌──────────┴─────────────────────────┐
     ▼                                    ▼
 [CLARIFY]                            [BLOCK]
   Re-plan with user input              Role / Policy denied
     │                                    │
     ▼                                    ▼
 [CONFIRM]                          [Terminated]
   Awaiting explicit authorization
     │
     ▼ (User confirms)
 [AUTO-APPROVE / APPROVED]
                │
                ▼
      ┌──────────────────┐
      │  Plan Integrity  │  ◄── Computes canonical action_hash & plan_id binding
      └─────────┬────────┘
                │
                ▼
      ┌──────────────────┐
      │ State Freshness  │  ◄── Pre-execution check: Does current state == planned before_state?
      └─────────┬────────┘      (Aborts to re-plan if resources drifted since planning)
                │
                ▼
      ┌──────────────────┐
      │Write-Ahead Journal│ ◄── Persists before_state & intended action before tool call
      └─────────┬────────┘
                │
                ▼
      ┌──────────────────┐
      │Controlled Executor│ ◄── Executes authorized MCP tools (State-changing execution capability is restricted to the Controlled Executor)
      └─────────┬────────┘
                │
                ▼
      ┌──────────────────┐
      │Post-Exec Verifier│  ◄── Checks deterministic invariants against actual system state
      └─────────┬────────┘
                │
        ┌───────┴───────┐
        ▼               ▼
    [SUCCESS]       [FAILURE / DRIFT]
   Record after-       │
   state in journal    ▼
             ┌─────────────────────┐
             │   Recovery Engine   │ ◄── Saga compensation for reversible actions
             └─────────────────────┘     Detects drift; flags human review if changed
```

---

## 2. Core Design Principles

1. **Hybrid Control Boundary:**  
   Probabilistic reasoning for understanding natural language; deterministic enforcement for permissions, policy, plan hashing, and state modification.
2. **The Agent Proposes; The Control Layer Decides:**  
   The LLM agent never possesses direct authorization to execute state-changing tools.
3. **Plan Before Policy:**  
   Risk cannot be assessed purely from user phrasing. The system must perform a read-only dry run to resolve concrete targets, affected items, and resource states before evaluating policy.
4. **Facts Over Self-Reported Confidence:**  
   LLM self-reported confidence scores are uncalibrated and treated merely as weak qualitative signals—never as an authorization threshold.
5. **No Universal Rollback Illusion:**  
   Arbitrary enterprise operations cannot be magically undone. CARE uses explicit, per-action **saga-style compensation** for supported reversible actions and mandates human escalation for irreversible or drifted effects.
6. **State Freshness & Drift Awareness:**  
   - *Pre-execution:* Validates that resources have not changed between dry-run planning and execution.
   - *Pre-compensation:* Validates that resources have not drifted between execution and rollback.
7. **Verification Over Tool Success:**  
   Receiving a successful response from an API does not prove intent satisfaction. Independent post-execution invariant verification is mandatory.

---

## 3. Probabilistic vs. Deterministic System Boundaries

| Probabilistic / LLM Tier | Deterministic / System-Enforced Tier |
| :--- | :--- |
| Natural-language intent interpretation | Role-Based Access Control (RBAC) & Permissions |
| Goal, entity, and constraint extraction | Deterministic Policy Gate (Auto-Approve, Clarify, Confirm, Block) |
| Candidate dry-run plan generation | Trusted tool metadata (reversibility, destructive, external impact) |
| LLM-assisted semantic verification *(optional extension)* | Plan Integrity & Replay Protection (`plan_id` + canonical `action_hash`) |
| | Pre-execution resource freshness validation |
| | Write-Ahead Action Journal (SQLite persistence) |
| | System state invariant checking |
| | Saga compensation & drift detection engine |

---

## 4. Policy Engine & Governance

### Policy Decision Outcomes

| Outcome | Trigger Condition & Meaning |
| :--- | :--- |
| **`AUTO-APPROVE`** | Clear scope, verified permissions, low-risk, internal-only, fully reversible, and within threshold limits. |
| **`CLARIFY`** | Scope ambiguity, external party affected without explicit instruction, or infeasible actions (e.g., calendar time conflicts). Triggers a conversational loop to re-plan. |
| **`CONFIRM`** | Well-understood plan, but consequential (destructive operations, external attendee effects, tagged escalations, or high-priority tickets). Requires explicit user sign-off. |
| **`BLOCK`** | Missing role permissions or violation of hard system security policies. Execution is terminated immediately with a descriptive rationale. |

### Concrete Policy Evaluation Precedence
To prevent unauthorized or ambiguous execution, the Policy Engine evaluates rules in strict order:
1. **Permission Check:** Does `user_role` have authority for all operations in the plan? *(If no → `BLOCK`)*
2. **Ambiguity & Feasibility:** Does the plan contain unresolved external participants or physical conflicts (e.g., overlapping meetings)? *(If yes → `CLARIFY`)*
3. **Consequential Action Rules:** Does any action carry high risk, destructive effects, external side effects, or escalation tags? *(If yes → `CONFIRM`)*
4. **Auto-Approval Rule:** Are all operations internal, authorized, reversible, within `affected_count <= threshold`, and low risk? *(If yes → `AUTO-APPROVE`)*

---

## 5. System Architecture & Components

### 1. Intent Parser Interface (Provider Integration Teammate-Owned)
Transforms unstructured user prompts into the frozen structured intent schema. The current runtime uses cached responses and deterministic offline pattern extraction; a real LLM provider is not integrated and remains teammate-owned.

### 2. Planner & Dry-Run Resolver
Utilizes read-only inspection tools to query current system state (e.g., reading calendar slots or ticket statuses). Generates concrete proposed actions populated with explicit targets, parameters, and current `before_state`. Unresolved or multiply matched targets produce no actions and return to policy for clarification. Calendar rescheduling preserves the event duration, uses its existing date unless an explicit supported date is provided, and retains its timezone.

### 3. Policy & Risk Engine
Deterministic rule evaluator operating on concrete action parameters and trusted tool metadata (`reversible`, `destructive`, `external_effect`, `affects_external_party`, `compensation_supported`). LLM-proposed values are advisory only.

### 4. Plan Integrity & Replay Protection Layer
Binds an approved plan to a unique server-side `plan_id` and an immutable `action_hash`.  
- **Action-List Mismatch Detection:** `action_hash` is computed using SHA-256 over canonical JSON (sorted keys, stripping dynamic runtime fields such as status). The executor validates that the action list to execute matches the approved plan byte-for-byte.
- **Single-Use Replay Protection:** A `plan_id` is strictly single-use. Upon execution commencement, the plan transitions to `EXECUTING` through an atomic database state change; concurrent or subsequent execution attempts are rejected. Authorization remains anchored to the stored approved plan record, not the hash in isolation.
- **Confirmation Binding:** For `CONFIRM`, the user authorizes the exact persisted `plan_id + action_hash`. The plan cannot be regenerated or modified after confirmation; any change requires a new plan and authorization.

### 5. Write-Ahead Action Journal
Maintains a durable action journal in SQLite with application-level audit preservation (leveraging SQLite WAL mode for database durability/concurrency) tracking the lifecycle of every operation:
1. **Pre-Write:** Logs `planned` action, parameters, and baseline `before_state`.
2. **Execution:** Transitions status to `executing`.
3. **Post-Write:** Logs resulting `after_state`, tool output `result`, or runtime `error`.
4. **Recovery Log:** Tracks compensation execution, completed compensations, and drift alerts.
5. **Unknown Outcome:** Records `UNKNOWN` when a tool may have been dispatched but its response was lost; reconciliation is required before retry or compensation.

### 6. Controlled Executor
The only component possessing write/execute authority against system tools in the application architecture. Enforces mandatory barriers before invoking any write tool:
- **Authorization & Single-Use Check:** Plan exists, is `APPROVED`, and has not been previously consumed.
- **Policy Outcome Check:** Stored policy outcome must be `AUTO_APPROVE` or `CONFIRM`; confirmation-required plans are persisted only after the explicit confirmation flow.
- **Integrity Validation:** `hash(actions) == approved_action_hash`.
- **State Freshness Check:** Re-reads each target resource immediately before its corresponding state-changing action; if `live_state != before_state`, aborts that action and requests re-planning.
- **Execution-Boundary Verification:** Checks action invariants immediately after each tool return and plan-level invariants before marking the plan `DONE`; failed verification enters the compensation flow.

### 7. Post-Execution Verifier
Executes invariant checks against actual post-operation resource states:
- Generic invariants: `expected_state_equals_actual_state`, `preserve_items`, `max_affected`, `no_destructive_actions`.
- Distinguishes technical execution (API returned HTTP 200) from intent correctness (state satisfies post-conditions).

### 8. Recovery & Compensation Engine
Implements a saga compensation pattern for multi-action failures:
- If action $k$ fails in a multi-step sequence, compensations are triggered in reverse order for actions $1 \dots k-1$.
- **Drift Protection:** Prior to running any compensation action, the engine inspects the resource. If `current_state != journaled after_state` (meaning an external user or system modified the resource after the agent touched it), the system **halts rollback** and flags the incident for **human review**. Compensation actions are predefined/approved as part of the original plan; the recovery engine cannot invent new compensation operations at runtime.
- **Interrupted Action Reconciliation:** Before compensation, any write-ahead record left `EXECUTING` or `UNKNOWN` is compared with its recorded `before_state` and the action's predicted `after_state`. A match is journaled as unapplied or completed; any third state is recorded as drift and halts recovery for human review.

---

## 6. Model Context Protocol (MCP) Integration

CARE runs one actual **FastMCP** server that exposes the existing calendar and ticket operations as namespaced MCP tools. `src/mcp/server.py` remains the CARE adapter for domain stores and trusted metadata; `src/mcp/fastmcp_server.py` exposes that adapter over MCP and is also used by the Controlled Executor through FastMCP's in-memory protocol client.

Run the standalone MCP server with `python -m src.mcp.fastmcp_server` (stdio transport). Read-only discovery calls are available for planning. Mutation tools validate the persisted plan and action, the active execution or recovery state, exact parameters, a write-ahead journal record, and a one-time dispatch capability whose hash is stored in SQLite. Domain stores also require an internal server authorization token. A `plan_id` and `action_hash` alone cannot authorize a mutation.

```
                      ┌──────────────────────────────────────┐
                      │            CARE MCP SERVER           │
                      │                                      │
                      │       calendar.*   tickets.*        │
                      └────────────┬──────────────┬──────────┘
                                   │              │
                          ┌────────▼───────┐ ┌────▼────────┐
                          │ Calendar domain│ │ Ticket domain│
                          │ Events, schedule│ │ Status, notes│
                          └────────────────┘ └─────────────┘
```

- **Namespaced Tool Modules:** Cleanly modularized backend domain handlers (`calendar.*` and `tickets.*`); no files domain is implemented in this prototype.
- **Separation of Privileges:**
  - The conversational agent is provided with **read-only inspection** tools and **plan proposal** capabilities.
  - State-changing tools require an authorized `plan_id` and verified `action_hash`, accessible exclusively by the CARE Executor.

---

## 7. Frozen Interface Contracts (JSON Schemas)

To enable seamless parallel development across teammates, these three core interface contracts are frozen as the baseline standard:

### Contract 1: Structured Intent
```json
{
  "goal": "Reschedule team meeting to avoid clash with project launch",
  "scope": "internal_calendar",
  "entities": [
    "team meeting",
    "project launch"
  ],
  "constraints": [
    {
      "type": "preserve_items",
      "source": "explicit",
      "params": {
        "property": "external_attendee"
      }
    }
  ],
  "ambiguities": [],
  "intent_confidence": "high"
}
```
> *Allowed constraint types:* `preserve_items`, `max_affected`, `no_destructive_actions`, `expected_state_equals_actual_state`.  
> *Note:* `intent_confidence` (`low` | `medium` | `high`) is strictly qualitative and never grants execution authority.

### Contract 2: Candidate / Approved Plan
```json
{
  "plan_id": "plan_982f1b8a-3e12",
  "intent": {
    "goal": "Move 3 PM internal sync to 5 PM",
    "scope": "calendar"
  },
  "actor": "user_mithun",
  "user_role": "STANDARD_USER",
  "actions": [
    {
      "action_id": "act_001",
      "resource_type": "calendar",
      "resource_id": "evt_meeting_3pm",
      "operation": "update_time",
      "parameters": {
        "start_time": "2026-10-02T17:00:00Z",
        "end_time": "2026-10-02T17:30:00Z"
      },
      "before_state": {
        "start_time": "2026-10-02T15:00:00Z",
        "end_time": "2026-10-02T15:30:00Z"
      },
      "risk_level": "low",
      "reversible": true,
      "compensation_action": {
        "operation": "update_time",
        "parameters": {
          "start_time": "2026-10-02T15:00:00Z",
          "end_time": "2026-10-02T15:30:00Z"
        }
      },
      "constraints": [
        {
          "type": "expected_state_equals_actual_state",
          "params": {}
        }
      ]
    }
  ],
  "action_hash": "c01df17ae2d6a4bfd5359ae854c45b22fc1ef29ee5c30b3c898fe09bf3280604",
  "policy_outcome": "auto_approve",
  "status": "approved"
}
```
> *Valid Plan Statuses:* `planned`, `awaiting_confirmation`, `approved`, `blocked`, `terminated`, `executing`, `done`, `failed`, `recovering`, `compensated`, `drift`.

### Contract 3: Action Journal Record (SQLite)
```json
{
  "plan_id": "plan_982f1b8a-3e12",
  "action_id": "act_001",
  "action_hash": "c01df17ae2d6a4bfd5359ae854c45b22fc1ef29ee5c30b3c898fe09bf3280604",
  "actor": "user_mithun",
  "user_role": "STANDARD_USER",
  "resource_type": "calendar",
  "resource_id": "evt_meeting_3pm",
  "operation": "update_time",
  "before_state": {
    "start_time": "2026-10-02T15:00:00Z"
  },
  "after_state": {
    "start_time": "2026-10-02T17:00:00Z"
  },
  "compensation_action": {
    "operation": "update_time",
    "parameters": {
      "start_time": "2026-10-02T15:00:00Z"
    }
  },
  "status": "done",
  "timestamp": "2026-10-01T22:45:00Z",
  "result": {
    "status": "success",
    "event_id": "evt_meeting_3pm"
  },
  "error": null
}
```

---

## 8. Demonstration Walkthrough (The 4 Demo Beats)

The prototype is designed to prove architectural robustness through four scripted demonstration beats:

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                                   DEMO SCENARIO BEATS                                  │
├──────┬────────────────────────────────────┬────────────────────────────────────────────┤
│ Beat │ Scenario Description               │ Key Concept & Architectural Proof          │
├──────┼────────────────────────────────────┼────────────────────────────────────────────┤
│  1   │ Clear Request + Conflict Check     │ Dry run detects calendar conflict at 5 PM. │
│      │ "Move my 3 PM meeting to 5 PM"     │ Policy triggers CLARIFY; user selects 4 PM;│
│      │                                    │ re-plans, auto-approves, and executes.     │
├──────┼────────────────────────────────────┼────────────────────────────────────────────┤
│  2   │ Ambiguous Consequential Scope      │ Planner uncovers an event with an external │
│      │ "Clear my afternoon"               │ VIP client. Policy triggers CLARIFY rather │
│      │                                    │ than making unstated assumptions.          │
├──────┼────────────────────────────────────┼────────────────────────────────────────────┤
│  3   │ Consequential Action & RBAC        │ Attempt to close an escalated ticket.      │
│      │ "Close escalated ticket #402"      │ STANDARD_USER requires CONFIRM.            │
│      │                                    │ READ_ONLY user is immediately BLOCKED.     │
├──────┼────────────────────────────────────┼────────────────────────────────────────────┤
│  4   │ Partial Failure, Recovery & Drift  │ Multi-action plan fails midway. System     │
│      │ Injected error during execution    │ executes saga rollback for action 1. Simu- │
│      │                                    │ lated drift on action 2 halts overwrite and│
│      │                                    │ flags human review.                        │
└──────┴────────────────────────────────────┴────────────────────────────────────────────┘
```

---

## 9. Defensibility & Boundary Rules (Honesty Framework)

Judges evaluate both technical capability and architectural honesty. The CARE team strictly adheres to these boundary claims:

### What CARE Delivers:
- Deterministic gating preventing blind execution of ambiguous LLM proposals.
- Structural verification that real system state satisfies constraints post-execution.
- Canonical action hashing detects action-list mismatch between the approved plan and the execution request.
- Resilient recovery preventing accidental corruption when systems drift.

### What CARE Explicitly Does NOT Claim:
- **No Guaranteed Intent Correctness:** Natural language has inherent ambiguity; CARE manages risk and clarifies rather than claiming psychic accuracy.
- **No Universal Rollback:** External emails, API webhooks, or deleted upstream resources cannot always be rolled back. CARE compensates what is safely reversible and flags what is not.
- **No Calibrated LLM Confidence:** LLM self-reported probability numbers are not mathematical truth; policy relies on concrete observable facts.
- **Not Production-Ready:** This is an architectural hackathon prototype validating core control principles using simulated backends. Production deployment requires distributed consensus, production IAM, and enterprise connectors.

---

## 10. Engineering Q&A Reference (Judge Defense Guide)

> **Q1: Why not let the LLM execute tools directly using function calling?**  
> **A:** LLMs are inherently probabilistic. Allowing direct execution creates an unbounded risk of hallucinated parameters, scope violations, and catastrophic side effects. CARE enforces that the LLM only proposes, while deterministic code gates and executes.

> **Q2: Why evaluate policy on the planned dry-run actions rather than the user's prompt?**  
> **A:** The risk of an action cannot be understood from words alone. A seemingly benign request like *"clean up outdated items"* might resolve in the dry run to deleting 5,000 production records. Policy must evaluate the concrete target resources and side effects.

> **Q3: How do you detect action-list tampering and prevent plan replay?**  
> **A:** CARE computes a canonical SHA-256 `action_hash` and compares it with the hash stored on the approved server-side plan, detecting action-list mismatch. The executor also atomically consumes the single-use `plan_id`, preventing replay. The hash alone is not an authorization credential.

> **Q4: What if an external system state changes between planning and execution?**  
> **A:** CARE performs a final **per-action state freshness check** immediately before each state-changing call. If the live state no longer matches that action's `before_state`, the action is aborted and the user is guided to re-plan. The plan is not blindly retried against changed state.

> **Q5: How does CARE handle partial failures across sequential actions?**  
> **A:** CARE’s durable action journal records the state before and after each action. If action 3 of 5 fails, the recovery engine inspects the journal and executes predefined, already-approved compensation actions in reverse order for completed actions. If a tool response is ambiguous, the action first enters `UNKNOWN` and is reconciled before any retry or compensation.

> **Q6: What happens if a resource is modified externally during a failure?**  
> **A:** CARE conducts a **drift check** before applying compensation. If `current_state != journaled after_state`, the system detects third-party modification, aborts automatic compensation, and flags the conflict for human intervention.

---

## 11. Team Work Plan & Overnight Milestones

### Role & Permission Matrix

| Role | Read/Inspect | Internal Reversible Write | Consequential Write |
| :--- | :---: | :---: | :---: |
| `READ_ONLY` | Yes | No | No |
| `STANDARD_USER` | Yes | Subject to policy | Confirmation when consequential |
| `ADMIN` | Yes | Subject to policy | Confirmation/policy still applies |

`actor` and `user_role` come from authenticated or simulated request context, not from LLM inference.

### Role Assignments

| Role & Focus | Primary Responsibilities | Definition of Done |
| :--- | :--- | :--- |
| **Member A — Orchestrator & Policy** | Intent parser schema, dry-run planner flow, policy gate, approval state machine | End-to-end request reaches `AUTO_APPROVE`, `CLARIFY`, `CONFIRM`, or `BLOCK` correctly. |
| **Member B — Tools & MCP Layer** | MCP server implementation, namespaced `calendar.*` and `tickets.*` domain handlers | Read-only target resolution and gated state-changing calls function reliably. |
| **Member C — Journal & Recovery** | SQLite write-ahead journal, canonical hashing, saga compensation, drift detection | Multi-action partial failure triggers compensation; drift is successfully flagged. |
| **Member D — UI, Integration & Demo** | Demo web dashboard/CLI, scenario seed data, verification display, failure injection | The 4 demo beats can be reliably driven and visualized from a single interface. |

### Overnight Milestones

- **Checkpoint 1 (12:00 AM) — The First Vertical Slice:**  
  Intent JSON generated $\to$ Dry run generated $\to$ Policy engine gates request $\to$ One calendar action executes only through approved plan $\to$ Write-Ahead Journal records transaction.
- **Checkpoint 2 (2:00 AM) — Core Engine Stability:**  
  Calendar flow stable across all four outcomes (`AUTO`, `CLARIFY`, `CONFIRM`, `BLOCK`) $\to$ Plan hashing and pre-execution freshness check operational $\to$ Post-execution verification working $\to$ Partial failure and basic compensation tested.
- **Checkpoint 3 (6:00 AM) — Generalization & Demo Polish:**  
  Tickets domain integrated $\to$ Drift detection demonstrated under simulated external change $\to$ RBAC permission case operational $\to$ All 4 demo beats rehearsed with seed data and fallbacks.

---

## 12. Getting Started & Repository Structure

```
nimbus-ai-hackathon/
├── README.md                               # Project entry point & documentation index
├── NIMBUS_2026_CARE_Hybrid_Updated.docx    # Reference design specification
├── src/                                    # Implementation source (under development)
│   ├── orchestrator/                       # Intent parsing, planning, and policy gate
│   ├── policy/                             # Deterministic rules & RBAC engine
│   ├── executor/                           # Controlled executor, plan integrity, freshness
│   ├── journal/                            # SQLite Write-Ahead Journal & drift checker
│   ├── mcp/                                # MCP server and domain modules (calendar, tickets)
│   └── verifier/                           # Post-execution invariant checks
└── tests/                                  # Demo beat test suite & scenario runners
```

Documentation authority is domain-specific: README is the project entry point; PRD defines requirements; DATA defines schemas/data contracts; API defines interfaces; DECISIONS records architectural decisions; PHASES defines the roadmap; LOGS records implementation history.

*Built with precision for the Nimbus AI Hackathon 2026.*
