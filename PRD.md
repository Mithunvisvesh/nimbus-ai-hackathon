# Product Requirements Document (PRD)

## Project: CARE (Context-Aware Reasoning & Execution)
**Event:** NIMBUS Hackathon 2026  
**Status:** Frozen Baseline (Design v1)  
**Document Version:** 1.0.0  

---

## 1. Executive Summary & Vision

Autonomous AI agents in enterprise environments face a critical reliability barrier: **tool-level execution success does not imply intent-level correctness.** Modern agents frequently make successful API calls (returning `200 OK`) that nonetheless cause operational damage due to unstated user assumptions, ambiguous scopes, or unexpected side effects.

**CARE (Context-Aware Reasoning & Execution)** is an enterprise control layer that decouples **probabilistic natural-language intent interpretation** from **deterministic action authorization and execution**. Under CARE:
- The LLM interprets requests and proposes candidate dry-run actions.
- A deterministic Policy & Risk Engine evaluates concrete plans against observable facts (permissions, risk, reversibility, affected scope).
- A Controlled Executor cryptographically validates plan integrity, verifies resource freshness, logs actions to an append-only write-ahead journal, and executes tools.
- A Verifier and Recovery Engine enforce deterministic invariants and execute saga-style compensation with drift detection.

---

## 2. Target Personas & User Scenarios

### Target Personas
1. **Enterprise Operations & IT Administrators:** Require strict auditability, role-based access control (RBAC), and guarantees that automated agents cannot bypass organizational safeguards.
2. **Knowledge Workers & Executives:** Delegate high-level tasks (*"reschedule my afternoon syncs"*, *"triage priority tickets"*) with varying levels of ambiguity without fearing catastrophic unconfirmed actions.
3. **Developers & Systems Integrators:** Require standardized, modular tool integration via the **Model Context Protocol (MCP)** with verifiable pre- and post-conditions.

### Key Operational Scenarios
- **Scenario 1 — Clear Request with Hidden Conflict:** An explicit reschedule request (*"Move 3 PM meeting to 5 PM"*) where the target slot is already booked. Dry-run detects the conflict and clarifies alternatives before touching live data.
- **Scenario 2 — Ambiguous Scope vs. Consequential Actions:**
  - *Ambiguous Scope ("Clear my afternoon"):* A broad command where targets include internal recurring syncs and a high-stakes external client meeting. Because the user's intended treatment of the client meeting is unspecified, policy gates it for `CLARIFY`.
  - *Explicit Consequential Request ("Cancel all afternoon meetings including the client review"):* The intent is unambiguous, but because canceling an external client meeting carries significant operational risk, policy requires `CONFIRM`.
- **Scenario 3 — Role-Based Permission Enactment:** An unauthorized user (*"READ_ONLY"*) attempts to close an escalated ticket. The policy engine blocks the action deterministically.
- **Scenario 4 — Partial Failure & External Drift:** A multi-step batch action fails at step 2. Step 1 is compensated, but if step 1's resource was modified by a human in the interim, the system halts compensation and alerts human supervisors.

---

## 3. Functional Requirements (FRs)

### FR1: Probabilistic Intent Parsing
- **FR1.1:** Accept natural language input and extract structured intent: `goal`, `scope`, `entities`, `constraints`, `ambiguities`, and qualitative `intent_confidence`.
- **FR1.2:** Enforce that `intent_confidence` (`low`, `medium`, `high`) is qualitative only and **cannot** grant authorization.
- **FR1.3:** Map user constraints into standard constraint types (`preserve_items`, `max_affected`, `no_destructive_actions`, `expected_state_equals_actual_state`).

### FR2: Read-Only Dry Run & Planning
- **FR2.1:** Inspect live resources using **read-only** tools to resolve concrete target IDs, baseline parameters, and `before_state`.
- **FR2.2:** Produce a `Candidate Plan` containing explicit action items, declared risk levels, reversibility flags, and compensation actions.
- **FR2.3:** Strictly prohibit state mutations during the planning phase.

### FR3: Deterministic Policy & Risk Engine
- **FR3.1:** Evaluate the candidate plan against observable parameters: user role, action reversibility, affected scope count, external attendee impact, and escalation tags.
- **FR3.2:** Execute rule evaluation in strict order:
  1. *Permission Verification:* User role vs. action required privilege. (Fail $\to$ `BLOCK`)
  2. *Ambiguity & Feasibility:* External attendee ambiguity or time conflict. (Fail $\to$ `CLARIFY`)
  3. *Consequential Actions:* Destructive, external side effects, or escalation tags. (Fail $\to$ `CONFIRM`)
  4. *Low-Risk Auto-Approval:* Reversible, internal, authorized, and within item count threshold. (Pass $\to$ `AUTO-APPROVE`)
- **FR3.3:** Output one of four outcomes: `AUTO-APPROVE`, `CLARIFY`, `CONFIRM`, `BLOCK`.

### FR4: Plan Integrity & Single-Use Replay Protection
- **FR4.1:** Compute `action_hash` via SHA-256 over canonical JSON of the actions list (sorted keys, stripping runtime mutable fields like `status`). This provides cryptographic mismatch detection to ensure the actions to be executed match the exact actions evaluated and approved.
- **FR4.2:** Enforce single-use replay protection: a `plan_id` can be executed exactly once. Execution is permitted only when:
  1. The plan exists in server storage and has `status == APPROVED`.
  2. The submitted `action_hash` matches the approved plan's stored `action_hash`.
  3. The requesting actor matches the authorized plan actor.
  4. Pre-execution resource freshness checks pass.
- **FR4.3:** Immediately upon initiating execution, the plan's status transitions to `EXECUTING`. Any subsequent execution attempt with the same `plan_id` is deterministically rejected (`PLAN_ALREADY_CONSUMED`).

### FR5: State Freshness Check
- **FR5.1:** Immediately before executing any action, re-read the target resource.
- **FR5.2:** Compare live state to planned `before_state`. If state drifted between planning and execution, abort and require re-planning.

### FR6: Controlled Execution via MCP
- **FR6.1:** Connect to a single MCP server hosting domain namespaces. The current prototype implements `calendar.*` and `tickets.*`; a `files.*` namespace remains future scope.
- **FR6.2:** In the application architecture, the conversational agent is provided only with read-only discovery and proposal tool schemas; execution capability is strictly restricted to the Controlled Executor module.
- **FR6.3:** State-changing tools demand a validated `plan_id` and verified `action_hash`, supplied exclusively by the Controlled Executor after passing policy, replay, and freshness gates.

### FR7: Write-Ahead Action Journaling
- **FR7.1:** Maintain a durable write-ahead action journal (append-only by application design) in SQLite.
- **FR7.2:** Persist `before_state` and action parameters **prior** to tool execution (`status: executing`).
- **FR7.3:** Persist `after_state`, tool output `result`, and `error` immediately following tool return.

### FR8: Post-Execution Verification
- **FR8.1:** Execute deterministic invariant checks against real post-execution state.
- **FR8.2:** Treat tool return status (`200 OK`) as an operational signal, not proof of goal satisfaction.

### FR9: Saga Compensation & Drift Detection
- **FR9.1:** On partial failure or verification failure, trigger reverse compensation for completed reversible steps.
- **FR9.2:** Conduct pre-compensation drift check: compare live state to journaled `after_state`.
- **FR9.3:** If drift is detected, abort automatic rollback and flag for human review.

---

## 4. Non-Functional Requirements (NFRs)

- **NFR1: Determinism:** Given the same candidate plan and policy rules, the Policy Engine must produce identical gate decisions 100% of the time.
- **NFR2: Latency:**
  - Dry-run & target resolution: $< 2.5\text{s}$ (including LLM call).
  - Deterministic policy gate & plan hashing: $< 50\text{ms}$.
  - Pre-execution freshness check: $< 100\text{ms}$.
- **NFR3: Auditability & Traceability:** Every state mutation must correlate to a user ID, actor, `plan_id`, `action_hash`, and before/after states in durable storage.
- **NFR4: Fault Reconciliation:** The write-ahead journal preserves intended actions and observed execution states so incomplete or interrupted executions can be reconciled after restart. If an execution was interrupted mid-flight (e.g. tool dispatched but response unreceived), recovery reconciles the external resource's live state against the journaled `before_state` and `after_state` before deciding whether compensation or retry is required.
- **NFR5: Modularity:** Domain capabilities must be isolatable into independent modules without altering core policy or journal engines.

---

## 5. Technology Stack

| Layer | Component | Technology Selected | Rationale |
| :--- | :--- | :--- | :--- |
| **Language** | Core Runtime | **Python 3.11+ / 3.12** | Native async support, rich LLM SDK ecosystem, fast hackathon iteration. |
| **Data Validation** | Schema & Contracts | **Pydantic v2** | Strict typing, fast JSON serialization, native OpenAPI & JSON schema export. |
| **Tool Protocol** | Tool Abstraction | **Model Context Protocol (MCP)** | Standardized agent-to-tool protocol; enables modular tool namespacing. |
| **Persistence** | Write-Ahead Journal | **SQLite + aiosqlite (WAL mode)** | Application-level write-ahead action lifecycle; SQLite WAL mode provides database engine concurrency and durability. |
| **Integrity & Hashing**| Mismatch Detection | **Python `hashlib` (SHA-256)** | Canonical JSON serialization (`json.dumps(..., sort_keys=True)`) for action-list mismatch detection. |
| **LLM Interface** | Intent & Dry Run | **LiteLLM / Gemini API / Claude SDK** | Provider-agnostic fallback support; structured outputs via JSON schema. |
| **Web / API Server**| Orchestrator Service | **FastAPI + Uvicorn** | High performance async endpoints for demo UI and test runner integration. |
| **Demo Interface** | Interactive UI | **Streamlit** (or Rich CLI) | Rapid interactive demoing of the 4 beats, manual drift injection, and audit view. |

---

## 6. Repository Structure

```
nimbus-ai-hackathon/
├── README.md                               # Project overview and executive summary
├── PRD.md                                  # Product Requirements Document & Architecture
├── PHASES.md                               # Implementation phases, checkpoints & ownership
├── DATA.md                                 # Data models, contracts, and SQLite schemas
├── API.md                                  # REST endpoints & MCP tool specifications
├── DECISIONS.md                            # Architecture Decision Records (ADRs)
├── AGENTS.md                               # AI agent coding guidelines & constraints
├── LOGS.md                                 # Execution audit logs & demo traces
├── pyproject.toml                          # Python dependencies and build packaging
├── .env.example                            # Configuration environment variables template
│
├── src/
│   ├── __init__.py
│   ├── config.py                           # App settings, environment configs, thresholds
│   │
│   ├── schemas/                            # Frozen JSON Contracts & Pydantic models
│   │   ├── __init__.py
│   │   ├── intent.py                       # StructuredIntent schema & Constraint models
│   │   ├── plan.py                         # PlannedAction, CandidatePlan, ApprovedPlan
│   │   ├── journal.py                      # JournalRecord, DriftIncident models
│   │   └── policy.py                       # PolicyOutcome, RuleEvaluation models
│   │
│   ├── intent/                             # Stage 1: Probabilistic interpretation
│   │   ├── __init__.py
│   │   ├── parser.py                       # LLM-backed intent extractor
│   │   └── prompts.py                      # System prompts & few-shot schema bindings
│   │
│   ├── planner/                            # Stage 2: Read-only dry run resolver
│   │   ├── __init__.py
│   │   └── dry_run.py                      # Resource state resolution & action generation
│   │
│   ├── policy/                             # Stage 3: Deterministic risk & policy gate
│   │   ├── __init__.py
│   │   ├── engine.py                       # 4-stage deterministic evaluation engine
│   │   └── rules.py                        # Concrete RBAC, ambiguity, and risk rules
│   │
│   ├── integrity/                          # Stage 4: Cryptographic hashing & freshness
│   │   ├── __init__.py
│   │   ├── hasher.py                       # Canonical SHA-256 action list hasher
│   │   └── freshness.py                    # Pre-execution resource freshness checker
│   │
│   ├── journal/                            # Stage 5: Write-ahead action logging
│   │   ├── __init__.py
│   │   ├── db.py                           # SQLite connection & table setup
│   │   └── writer.py                       # Append-only journal writer & query API
│   │
│   ├── executor/                           # Stage 6: Controlled execution
│   │   ├── __init__.py
│   │   └── runner.py                       # Gated tool executor (verifies hash & plan_id)
│   │
│   ├── verifier/                           # Stage 7: Post-execution invariant checker
│   │   ├── __init__.py
│   │   └── invariant_checker.py            # Invariant validator against live resources
│   │
│   ├── recovery/                           # Stage 8: Saga compensation & drift engine
│   │   ├── __init__.py
│   │   ├── compensation.py                 # Reverse saga compensation runner
│   │   └── drift_detector.py               # Pre-compensation drift verification
│   │
│   └── mcp/                                # Tool Provider Layer
│       ├── __init__.py
│       ├── server.py                       # FastMCP server router
│       └── domains/                        # Namespaced domain modules
│           ├── __init__.py
│           ├── calendar_module.py          # Calendar mock & state store
│           ├── tickets_module.py           # Ticket mock & RBAC state store
│           └── files_module.py             # Optional file system mock
│
├── ui/                                     # Demo Interface
│   ├── app.py                              # Streamlit interactive control center
│   └── components/                         # UI views (Pipeline graph, Journal table, etc.)
│
├── tests/                                  # Test Suite
│   ├── test_intent.py                      # Intent schema & constraint parsing tests
│   ├── test_policy.py                      # Deterministic policy outcome tests
│   ├── test_integrity.py                   # Canonical hashing & anti-tamper tests
│   ├── test_freshness.py                   # Pre-execution freshness abort tests
│   ├── test_journal.py                     # SQLite WAL & write-ahead ordering tests
│   ├── test_compensation.py                # Saga rollback & drift detection tests
│   └── test_demo_beats.py                  # Integration tests for Beats 1, 2, 3, 4
│
└── demo/                                   # Seed Data & Scenarios
    ├── seed_calendar.json                  # Conflict & VIP meeting test fixtures
    ├── seed_tickets.json                   # Escalation & standard ticket fixtures
    └── run_beat.py                         # CLI script to execute specific demo beats
```

---

## 7. Hackathon Evaluation & Success Metrics

| Evaluation Pillar | Target Hackathon Criteria | CARE Demonstration Proof |
| :--- | :--- | :--- |
| **Safety & Control** | Agents must not run wild on ambiguous or high-risk inputs. | Beat 2 (VIP attendee triggers `CLARIFY`); Beat 3 (Unauthorized ticket closure `BLOCKED`). |
| **Resilience & Recovery** | Partial failures must be gracefully contained and audited. | Beat 4 (Failure triggers saga rollback; external drift prevents blind overwrite). |
| **Architectural Rigor** | No hardcoded scenarios; principled separation of concerns. | Universal 3-tier architecture (Intent $\to$ Policy $\to$ Executor) with canonical hashing. |
| **Generalization** | Solution works beyond a single calendar tool. | Native dual-domain validation across `calendar.*` and `tickets.*` modules. |
