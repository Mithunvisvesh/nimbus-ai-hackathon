# Architecture Decision Records (ADRs)

## Project: CARE (Context-Aware Reasoning & Execution)
**Event:** NIMBUS Hackathon 2026  
**Document Version:** 1.0.0  
**Status:** Approved & Frozen  

---

### Index of Decisions
- [ADR-001: Hybrid Control Architecture (Probabilistic Interpretation vs. Deterministic Enforcement)](#adr-001-hybrid-control-architecture)
- [ADR-002: Plan-Before-Policy (Evaluating Concrete Targets vs. Raw User Prompts)](#adr-002-plan-before-policy)
- [ADR-003: Rejection of LLM Self-Reported Confidence as an Authorization Gate](#adr-003-rejection-of-llm-self-reported-confidence)
- [ADR-004: Cryptographic Plan Integrity via Canonical JSON Hashing](#adr-004-cryptographic-plan-integrity)
- [ADR-005: Pre-Execution State Freshness Check](#adr-005-pre-execution-state-freshness-check)
- [ADR-006: Write-Ahead Action Journaling via SQLite WAL](#adr-006-write-ahead-action-journaling)
- [ADR-007: Saga-Style Compensation vs. The "Universal Rollback" Myth](#adr-007-saga-style-compensation)
- [ADR-008: Pre-Compensation Drift Detection](#adr-008-pre-compensation-drift-detection)
- [ADR-009: Single Namespaced MCP Server with Privileged Executor Separation](#adr-009-single-namespaced-mcp-server)
- [ADR-010: Defensibility & Honesty Framework (Boundary Claims)](#adr-010-defensibility--honesty-framework)

---

### ADR-001: Hybrid Control Architecture
- **Status:** Accepted
- **Context:** Autonomous agents often combine reasoning and tool execution in a single loop (e.g. ReAct / Tool Use). When an LLM hallucinates or misinterprets an unstated assumption, it immediately executes destructive operations.
- **Decision:** Separate the system into a **Probabilistic Interpretation Layer** (LLM produces structured intent and candidate plans) and a **Deterministic Enforcement Layer** (Policy, RBAC, Hashing, Journaling, Execution).
- **Consequences:** The LLM never possesses direct credentials to invoke state-changing tools. All state-changing tools are gated behind the deterministic executor.

---

### ADR-002: Plan-Before-Policy
- **Status:** Accepted
- **Context:** Conventional guardrails inspect the user's natural language input directly. However, a benign phrase like *"Clear my afternoon"* might mean deleting 1 internal sync or canceling a critical multi-million-dollar client meeting.
- **Decision:** Never evaluate policy purely on prompt text. Run a read-only dry run first to resolve the **concrete actions**, target resource IDs, and real `before_state`. Pass the resulting concrete candidate plan to the Policy Engine.
- **Consequences:** Risk is measured by what the system will *actually touch*, not how persuasively the user phrased the request.

---

### ADR-003: Rejection of LLM Self-Reported Confidence
- **Status:** Accepted
- **Context:** Many systems ask LLMs to output a confidence score (e.g., `confidence: 0.95`) and auto-execute if above 0.8.
- **Decision:** Treat LLM confidence strictly as an uncalibrated, qualitative signal (`low`, `medium`, `high`). **Never** use confidence as an authorization threshold.
- **Consequences:** Authorization is governed entirely by observable facts: permissions, action reversibility, affected resource counts, external attendee flags, and explicit user confirmations.

---

### ADR-004: Cryptographic Plan Integrity
- **Status:** Accepted
- **Context:** In multi-step or asynchronous agent loops, approved plans can suffer from prompt injection, parameter tampering, or replay attacks.
- **Decision:** When a plan is approved, bind it to a `plan_id` and compute an `action_hash` using SHA-256 over canonical JSON of the actions list (keys sorted, excluding mutable status fields). The executor recomputes this hash before executing any tool call.
- **Consequences:** Guarantees that the actions executed are byte-for-byte identical to the actions authorized by the policy engine or user.

---

### ADR-005: Pre-Execution State Freshness Check
- **Status:** Accepted
- **Context:** In enterprise systems, resources change dynamically. If a user asks an agent to reschedule a meeting, and another human cancels or moves it in the meantime, the agent will execute against stale data.
- **Decision:** Immediately before executing any action, the executor queries the resource's current state. If `current_state != before_state`, the plan is marked stale, execution is aborted, and the workflow loops back to planning.
- **Consequences:** Prevents race conditions and blind overwrites in multi-user collaborative environments.

---

### ADR-006: Write-Ahead Action Journaling
- **Status:** Accepted
- **Context:** Multi-step agent executions frequently suffer network drops, server restarts, or tool timeouts midway through execution, leaving enterprise systems in an unknown, corrupted state.
- **Decision:** Implement a write-ahead log in SQLite WAL mode. Before any tool call is dispatched, the intended action and its `before_state` are written to durable storage (`status: executing`). Once the tool responds, `after_state` and output results are written (`status: done`).
- **Consequences:** Every action is traceable. In case of sudden crash or failure, the system can inspect the journal and know precisely what executed and what needs compensation.

---

### ADR-007: Saga-Style Compensation vs. Universal Rollback
- **Status:** Accepted
- **Context:** Many agent frameworks claim "automatic rollback", which is technically dishonest for enterprise systems (e.g., external calendar invitations or webhook notifications cannot be un-sent).
- **Decision:** Adopt saga-style compensation. Each action explicitly declares whether it is `reversible` and provides a concrete `compensation_action`. Irreversible or externally visible actions require explicit upfront `CONFIRM`.
- **Consequences:** Reversible actions are compensated backwards sequentially on failure; irreversible actions are flagged honestly for human escalation.

---

### ADR-008: Pre-Compensation Drift Detection
- **Status:** Accepted
- **Context:** During failure recovery, blindly applying compensation can overwrite newer, legitimate modifications made by external human users or other services.
- **Decision:** Before executing a compensation action, check: `current_state == journaled after_state`. If they match, compensation proceeds safely. If they differ (drift detected), the system halts automatic rollback and logs an alert for human review.
- **Consequences:** Eliminates the catastrophic risk of an agent's rollback corrupting a human's manual correction.

---

### ADR-009: Single Namespaced MCP Server
- **Status:** Accepted
- **Context:** Managing multiple independent MCP process daemons during a hackathon demo introduces process management overhead and port collision risks.
- **Decision:** Host a single FastMCP server that exposes namespaced tools (`calendar.*`, `tickets.*`, `files.*`) backed by separate Python modules.
- **Consequences:** Radically simplifies deployment and demonstration while preserving modular architecture and domain isolation.

---

### ADR-010: Defensibility & Honesty Framework
- **Status:** Accepted
- **Context:** Hackathon judges penalize overclaiming (e.g. claiming 100% intent accuracy or universal rollback).
- **Decision:** Explicitly document in all project artifacts what CARE does NOT claim:
  1. *No guaranteed intent correctness.*
  2. *No universal rollback.*
  3. *No calibrated LLM confidence.*
  4. *Not claiming ticket closure is irreversible.*
  5. *Not claiming production-readiness.*
- **Consequences:** Elevates credibility and aligns directly with enterprise engineering standards.
