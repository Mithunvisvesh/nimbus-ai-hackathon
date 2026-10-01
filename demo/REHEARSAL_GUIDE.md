# CARE: Final Rehearsal & Judge Defense Guide

> **Nimbus AI Hackathon 2026 — Phase 4 Deliverable**  
> *A bullet-proof, 3-minute presentation script and architectural defense guide.*

---

## 1. The 3-Minute Live Judging Pitch (Verbatim Script)

### [0:00 - 0:30] — The Hook & The Problem
> *"Judges, autonomous AI agents are fantastic at calling tools, but dangerously bad at satisfying user intent. When you tell an assistant 'clear my afternoon', it will happily return HTTP `200 OK` while silently canceling a multi-million-dollar meeting with an external VIP client. Tool-level execution success does NOT mean intent-level correctness.*
> 
> *Direct function-calling gives probabilistic models unsupervised write authority. If the model hallucinates or encounters an unstated assumption, catastrophic side effects happen instantly."*

### [0:30 - 1:00] — The CARE Solution & Thesis
> *"We built **CARE**: Context-Aware Reasoning & Execution.*
> 
> *Our core architectural thesis is simple: **The LLM proposes; the control layer decides; the executor acts.**
> The LLM only ever outputs structured candidate plans. It never touches write APIs. Authorization is deterministically governed by an enterprise policy engine, cryptographically fingerprinted via SHA-256 canonical hashing, logged to a SQLite write-ahead journal, and verified through post-execution invariants."*

### [1:00 - 2:15] — The 4 Demonstration Beats (Click Through the UI)

#### Beat 1: Clear Request + Conflict Check (Click Beat 1 Button)
> *"Watch Beat 1. We ask: 'Move my 3 PM meeting to 5 PM'.*  
> *Notice what CARE does: before touching live data, it runs a **read-only dry run**. It detects that 5 PM is occupied by Strategy Review. Instead of blind execution, the policy gate intercepts with **`CLARIFY`** and suggests 4 PM and 6 PM. The user picks 4 PM, CARE re-plans, automatically verifies zero conflicts, auto-approves, and updates the calendar."*

#### Beat 2: Ambiguous Consequential Scope (Click Beat 2 Button)
> *"Beat 2 demonstrates risk awareness. We ask: 'Clear my afternoon'.*  
> *The dry run discovers three events, including an external client review with a VP. Rather than guessing, CARE's policy engine flags unstated external impact and triggers **`CLARIFY`**, protecting high-stakes relationships from blind cancellation."*

#### Beat 3: Consequential RBAC & Tickets (Click Beat 3 Button)
> *"Beat 3 proves multi-domain generalization. We ask: 'Close escalated ticket #402'.*  
> *If an intern with `READ_ONLY` role attempts this, the policy engine immediately returns **`BLOCK`**. When a `STANDARD_USER` attempts it, the policy recognizes the ticket has an active escalation tag and triggers **`CONFIRM`**. Only when a human signs off does the Controlled Executor consume the single-use plan and close the ticket."*

#### Beat 4: Multi-Step Recovery & Pre-Compensation Drift (Click Beat 4 Button)
> *"Beat 4 is our technical differentiator: **Drift-Aware Saga Recovery**.*  
> *A 2-step plan executes Step 1. Then an external human colleague reschedules that meeting directly. Step 2 fails. Most agent frameworks blindly roll back, overwriting the human's manual correction. CARE conducts a **pre-compensation drift check**: it sees the live state no longer matches the journaled after-state, halts rollback, marks status as `DRIFT`, and flags an incident for human review."*

### [2:15 - 3:00] — Defensibility & Wrap-Up
> *"CARE doesn't claim psychic intent accuracy or magic universal rollback. We claim architectural rigor: deterministic gating, single-use plan replay protection, pre-execution freshness, write-ahead durability, and drift protection.*  
> *Thank you, and we're ready for your questions!"*

---

## 2. Fast Command Reference for the Presentation

| Action | Command |
|:---|:---|
| **Launch Interactive UI** | `streamlit run ui/app.py` |
| **Run All 4 Beats in CLI** | `python demo/run_beat.py --all` |
| **Run Single Beat (e.g. Beat 3)** | `python demo/run_beat.py --beat 3` |
| **Run Full Test Suite (25 tests)** | `python -m pytest tests/ -v` |

---

## 3. Judge Defense Q&A Cheat Sheet (Bullet-Proof Answers)

### Q1: *"Why not just use OpenAI Function Calling or LangChain tools directly?"*
**Answer:**  
Function calling couples reasoning and tool execution into a single probabilistic loop. If the model hallucinates parameters or misinterprets an unstated assumption, side effects happen before any human or guardrail can intervene. CARE decouples reasoning (dry run proposal) from execution (deterministic policy gate + controlled executor).

### Q2: *"Why evaluate policy on the dry run rather than prompt text?"*
**Answer:**  
You cannot measure risk from words alone. *"Clean up old items"* sounds harmless, but in the dry run it might resolve to deleting 5,000 production records. Policy must evaluate observable facts: the exact resource IDs, attendee types, before-state, and operational reversibility.

### Q3: *"How do you prevent duplicate execution (replay attacks)?"*
**Answer:**  
Every plan has a unique `plan_id` and a canonical SHA-256 `action_hash`. Upon execution, the Controlled Executor performs an atomic Compare-And-Set (`UPDATE approved_plans SET status = 'executing' WHERE plan_id = ? AND status = 'approved'`). If the plan was already consumed or concurrently invoked, subsequent attempts are rejected with `PLAN_ALREADY_CONSUMED`.

### Q4: *"What if system state changes between planning and execution?"*
**Answer:**  
CARE enforces a **pre-execution freshness check** immediately before each state-changing call. If the live resource's current state differs from the dry-run `before_state`, execution aborts with `STALE_RESOURCE_STATE` and prompts the user to re-plan.

### Q5: *"Why reject universal rollback?"*
**Answer:**  
In enterprise systems, true universal rollback is technically dishonest: external emails cannot be un-sent, webhooks cannot be un-fired, and external humans may have modified the data in the meantime. CARE uses saga-style compensation only for declared reversible operations, and halts rollback if drift is detected.

---

*Prepared for the Nimbus AI Hackathon 2026 Judging Panel.*
