# Implementation Phases & Milestone Roadmap

## Project: CARE (Context-Aware Reasoning & Execution)
**Event:** NIMBUS Hackathon 2026  
**Execution Window:** Overnight Hackathon Sprint (Rounds 1 $\to$ Round 2 $\to$ Final Prototype)  

### Current Prototype Status (2026-10-02)
- Checkpoints 1–4 are implemented in the local prototype; the test suite passes.
- The tool layer now uses FastMCP. Mutation authorization is bound to persisted actions and one-time executor/recovery capabilities.
- The real LLM provider integration is still teammate-owned and is not complete; cached and deterministic offline parsing remain available.

---

## 1. Team Ownership & Responsibilities

The workload is partitioned across four specialized functional roles to enable concurrent development without merge contention.

| Role | Domain Scope | Primary Deliverables | Definition of Done (DoD) |
| :--- | :--- | :--- | :--- |
| **Member A**<br>*Orchestrator & Policy* | Intent Schema, Planner, Policy Engine, Gate | `src/intent/`<br>`src/planner/`<br>`src/policy/` | Natural language prompt generates valid intent schema; dry run resolves concrete targets; policy deterministically produces `AUTO-APPROVE`, `CLARIFY`, `CONFIRM`, or `BLOCK`. |
| **Member B**<br>*Tools & MCP Server* | FastMCP Server, Domain Modules, Permissions | `src/mcp/`<br>`src/mcp/domains/`<br>`demo/seed_*.json` | MCP server exposes namespaced `calendar.*` and `tickets.*` tools; read-only operations resolve state; state-changing calls reject unauthorized callers. |
| **Member C**<br>*Journal & Recovery* | Plan Hashing, SQLite WAL, Saga Compensation, Drift | `src/integrity/`<br>`src/journal/`<br>`src/recovery/` | Write-ahead records persist before/after states; canonical action hash detects modifications; compensation triggers on failure; drift stops rollback and alerts. |
| **Member D**<br>*UI, Integration & Demo* | Streamlit UI, Demo Runner, Invariant Verifier | `ui/`<br>`src/verifier/`<br>`tests/test_demo_beats.py` | Interactive dashboard renders pipeline state graph, journal table, and manual drift controls; all 4 demo beats pass reliably. |

---

## 2. Milestone Phases & Checkpoints

```
  Phase 0: Contract Freeze (Pre-Round 2 - By 9:45 PM)
     │
     ▼
  Phase 1: Checkpoint 1 (12:00 AM) — Vertical Slice MVP
     │
     ▼
  Phase 2: Checkpoint 2 (02:00 AM) — Core Stability & Resilience
     │
     ▼
  Phase 3: Checkpoint 3 (06:00 AM) — Generalization & Polish
     │
     ▼
  Phase 4: Final Rehearsal & Defense (Morning - Judging)
```

---

### Phase 0: Contract Freeze & Workspace Scaffolding (By 9:45 PM)
**Objective:** Lock interface schemas and directory structure so all members can develop in parallel.

- [x] Freeze the 3 JSON contracts: `StructuredIntent`, `Candidate/Approved Plan`, `JournalRecord`.
- [x] Establish the 4 canonical constraint types: `preserve_items`, `max_affected`, `no_destructive_actions`, `expected_state_equals_actual_state`.
- [x] Scaffold repository folder structure and virtual environment setup.
- [x] Team consensus on contract shapes (Zero breaking schema edits after 9:45 PM).

---

### Phase 1: Checkpoint 1 (12:00 AM) — The First Vertical Slice
**Objective:** End-to-end execution of a single authorized calendar action through the entire gated pipeline.

#### Member Deliverables:
- **Member A:**
  - Build `IntentParser` using LLM structured output.
  - Implement dry-run target resolution for calendar events.
  - Implement baseline `PolicyEngine` evaluating permission and low-risk rules.
- **Member B:**
  - Scaffold FastMCP server with `calendar.list_events` and `calendar.update_event`.
  - Provide in-memory mock store initialized from `demo/seed_calendar.json`.
- **Member C:**
  - Initialize SQLite journal with write-ahead schema (`journal_records`).
  - Implement `hasher.py` calculating SHA-256 over canonical plan action list.
  - Implement pre-action journal write (`status: executing`).
- **Member D:**
  - Create integration test script `tests/test_checkpoint1.py`.
  - Verify sequential flow: `Prompt` $\to$ `Intent` $\to$ `Dry Run` $\to$ `Auto-Approve` $\to$ `Journal Pre-Write` $\to$ `Execute` $\to$ `Journal Post-Write`.

**Phase 1 Gate Criteria (DoD):**
1. Running `"Move 3 PM meeting to 4 PM"` generates valid JSON.
2. Tool execution occurs **only** after policy approval.
3. SQLite database contains an audit record with populated `before_state` and `after_state`.

---

### Phase 2: Checkpoint 2 (02:00 AM) — Core Stability & Resilience
**Objective:** Complete all 4 policy outcomes, pre-execution freshness verification, post-execution invariant verification, and partial failure handling.

#### Member Deliverables:
- **Member A:**
  - Implement `CLARIFY` loop for time conflicts and unstated scope ambiguities.
  - Implement `CONFIRM` loop for high-risk and external actions.
  - Implement `BLOCK` for missing privileges.
- **Member B:**
  - Implement token/hash verification on state-changing MCP tool endpoints.
  - Add failure-injection flag to MCP tools (`simulate_failure=True`).
- **Member C:**
  - Implement `FreshnessChecker`: verify `live_resource == before_state` prior to execution.
  - Implement `SagaCompensationRunner`: execute reverse compensation on multi-step failure.
- **Member D:**
  - Implement `InvariantChecker`: deterministic post-execution state validator.
  - Initialize basic Streamlit dashboard showing:
    - Current Plan Status
    - Live Pipeline Diagram
    - Action Journal Log

**Phase 2 Gate Criteria (DoD):**
1. Beat 1 works: Time conflict produces `CLARIFY`, user chooses alternative time, re-plans, and executes.
2. Beat 2 works: External attendee raises risk and produces `CLARIFY` / `CONFIRM`.
3. Injected failure on Step 2 of a 2-step plan triggers reverse compensation for Step 1.

---

### Phase 3: Checkpoint 3 (06:00 AM) — Generalization, Drift & Demo Polish
**Objective:** Prove generalization beyond calendar with Tickets domain, demonstrate drift detection during recovery, and polish live demo UI.

#### Member Deliverables:
- **Member A:**
  - Add Ticket domain intent extraction and ticket triage planning rules.
  - Implement RBAC policy: `READ_ONLY` role attempting ticket mutation $\to$ `BLOCK`.
- **Member B:**
  - Implement `tickets.*` MCP module (`get_ticket`, `list_tickets`, `update_status`, `reopen_ticket`).
  - Populate `demo/seed_tickets.json` with standard and escalated tickets.
- **Member C:**
  - Implement `DriftDetector`: verify `current_state == journaled after_state` prior to compensation.
  - If drift detected, mark action as `status: drift`, halt rollback, and log alert.
- **Member D:**
  - Build UI demo controls for:
    - *Simulate External Edit* (triggers drift).
    - *Inject Tool Failure* (triggers compensation).
    - *Switch User Role* (`ADMIN`, `STANDARD_USER`, `READ_ONLY`).
  - Script end-to-end automation for Beats 1, 2, 3, and 4.

**Phase 3 Gate Criteria (DoD):**
1. Beat 3 works: `STANDARD_USER` requires `CONFIRM` for escalated ticket; `READ_ONLY` user is `BLOCKED`.
2. Beat 4 works: Step 2 fails, Step 1 resource is externally edited before compensation $\to$ system flags `DRIFT` and aborts blind overwrite.
3. All 4 beats are executable in $< 3$ minutes from the UI.

---

### Phase 4: Final Rehearsal & Defense (Morning / Pre-Judging)
**Objective:** Stress-test live presentation, prepare contingency fallbacks, and rehearse judge defense.

- [ ] Rehearse the 4-Beat live demonstration end-to-end.
- [ ] Prepare static fallback fixtures in case venue Wi-Fi or LLM API rates spike.
- [ ] Review the **Judge Q&A Defense Guide** in `README.md` (why plan before policy, why not LLM confidence, why no universal rollback).
- [ ] Verify clean, reproducible setup command: `python demo/run_beat.py --beat [1|2|3|4]`.

---

## 3. Contingency & Risk Management

| Risk Event | Severity | Mitigation Strategy |
| :--- | :--- | :--- |
| **LLM Provider API Outage or Rate Limit** | High | Use LiteLLM with automatic fallback from primary (Gemini/Claude) to secondary (OpenAI/Local Ollama); cache deterministic demo prompt responses in `demo/cached_llm_responses.json`. |
| **Venue Wi-Fi Degradation** | High | Entire demo must be capable of running 100% offline via local mock MCP server and cached/local LLM fixtures. |
| **Schema Merge Conflict between Teammates** | Medium | Contracts are locked in `src/schemas/`. Schema changes require unanimous team sign-off. Modules interact solely through these frozen schemas. |
| **Demo Timing Constraints (<5 min)** | Medium | Single-click demo buttons in Streamlit UI pre-populate prompts and simulate realistic execution beats instantaneously. |
