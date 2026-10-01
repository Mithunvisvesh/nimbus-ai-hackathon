# System Execution Logs & Audit Trail (LOGS.md)

## Project: CARE (Context-Aware Reasoning & Execution)
**Event:** NIMBUS Hackathon 2026  
**Document Version:** 1.0.0  

---

## 1. Audit Trail & Log Architecture

CARE enforces strict auditability. Every agent operation leaves a tamper-evident audit trace across three complementary tiers:

1. **System Telemetry Logs (Structured Console & File Output):** Timestamped JSON lines capturing component transitions, latency, and policy evaluations.
2. **Write-Ahead Action Journal (SQLite WAL):** Durable, ACID-compliant database tracking `before_state`, `after_state`, parameters, and status for every state-changing action.
3. **Drift & Recovery Incidents Log:** Explicit event logging when compensation halts due to external drift.

```
 [User Prompt] ──────────────────────────────────────────► Console / File Log
      │                                                         │
      ▼                                                         ▼
 [Intent & Plan Generated] ──────────────────────────────► Structured Trace
      │                                                         │
      ▼                                                         ▼
 [Policy Gate Evaluated] ────────────────────────────────► Audit Log
      │                                                         │
      ▼                                                         ▼
 [Write-Ahead Journal Record Written (status: executing)] ─► SQLite WAL Database
      │                                                         │
      ▼                                                         ▼
 [Tool Executed & Result Verified] ───────────────────────► SQLite WAL Database
      │                                                         │
      ▼                                                         ▼
 [Recovery Triggered / Drift Detected] ───────────────────► Incident Log
```

---

## 2. Project Change Log (Milestones & Evolution)

### [Milestone 0] — 2026-10-01 22:45 UTC+5:30: Specification & Contract Freeze
- Analyzed `NIMBUS_2026_CARE_Hybrid_Updated.docx` (Source of Truth).
- Updated [`README.md`](README.md) with comprehensive architecture specification and demo beats.
- Froze the 3 core JSON schemas in [`DATA.md`](DATA.md): `StructuredIntent`, `Candidate/Approved Plan`, `JournalRecord`.
- Established [`PRD.md`](PRD.md), [`PHASES.md`](PHASES.md), [`API.md`](API.md), [`DECISIONS.md`](DECISIONS.md), and [`AGENTS.md`](AGENTS.md).
- Repository pushed and synchronized to GitHub `main` branch.

### [Milestone 1] — Checkpoint 1 (Target: 12:00 AM): Vertical Slice MVP
- *Status:* Pending implementation.
- *Focus:* Intent JSON generation $\to$ Dry run target resolution $\to$ Policy auto-approval $\to$ Single calendar event execution via Write-Ahead Journal.

### [Milestone 2] — Checkpoint 2 (Target: 02:00 AM): Core Engine Stability & Resilience
- *Status:* Pending implementation.
- *Focus:* All 4 policy outcomes (`AUTO`, `CLARIFY`, `CONFIRM`, `BLOCK`) $\to$ Plan hashing & pre-execution freshness $\to$ Deterministic verification $\to$ Partial failure injection & saga rollback.

### [Milestone 3] — Checkpoint 3 (Target: 06:00 AM): Generalization & Demo Polish
- *Status:* Pending implementation.
- *Focus:* Tickets domain integration $\to$ Pre-compensation drift detection under external modification $\to$ RBAC permission case $\to$ Streamlit live demo dashboard.

---

## 3. Reference Demo Execution Traces (The 4 Beats)

The following reference traces demonstrate the expected log output for the four core hackathon scenarios.

---

### Beat 1: Clear Request + Conflict Check (Reschedule 3 PM Meeting)

```json
{"timestamp": "2026-10-02T00:15:02.102Z", "component": "intent_parser", "event": "INTENT_EXTRACTED", "goal": "Move 3 PM meeting to 5 PM", "scope": "calendar", "intent_confidence": "high"}
{"timestamp": "2026-10-02T00:15:02.540Z", "component": "planner_dry_run", "event": "TARGET_RESOLVED", "resource_id": "evt_3pm_sync", "before_state": {"start_time": "15:00", "end_time": "15:30"}}
{"timestamp": "2026-10-02T00:15:02.610Z", "component": "planner_dry_run", "event": "CONFLICT_DETECTED", "target_time": "17:00", "conflicting_event": "evt_5pm_hold", "title": "Strategy Review"}
{"timestamp": "2026-10-02T00:15:02.630Z", "component": "policy_engine", "event": "EVALUATION_COMPLETE", "outcome": "CLARIFY", "reason": "Target slot 5:00 PM is already occupied by 'Strategy Review'. Suggested: 16:00."}
{"timestamp": "2026-10-02T00:15:05.100Z", "component": "orchestrator", "event": "USER_INPUT_RECEIVED", "selection": "16:00"}
{"timestamp": "2026-10-02T00:15:05.320Z", "component": "policy_engine", "event": "EVALUATION_COMPLETE", "outcome": "AUTO_APPROVE", "reason": "No conflict, internal attendees, authorized."}
{"timestamp": "2026-10-02T00:15:05.350Z", "component": "plan_integrity", "event": "PLAN_SEALED", "plan_id": "plan_b710f4", "action_hash": "a1f9c84e931b..."}
{"timestamp": "2026-10-02T00:15:05.380Z", "component": "freshness_checker", "event": "FRESHNESS_VERIFIED", "resource_id": "evt_3pm_sync", "status": "MATCH"}
{"timestamp": "2026-10-02T00:15:05.410Z", "component": "journal", "event": "WRITE_AHEAD_LOGGED", "plan_id": "plan_b710f4", "action_id": "act_001", "status": "executing"}
{"timestamp": "2026-10-02T00:15:05.620Z", "component": "executor", "event": "TOOL_INVOKED", "tool": "calendar.update_event", "result": "HTTP 200 OK"}
{"timestamp": "2026-10-02T00:15:05.650Z", "component": "journal", "event": "WRITE_AHEAD_UPDATED", "action_id": "act_001", "status": "done", "after_state": {"start_time": "16:00"}}
{"timestamp": "2026-10-02T00:15:05.680Z", "component": "verifier", "event": "INVARIANT_VERIFIED", "invariant": "expected_state_equals_actual_state", "status": "PASS"}
```

---

### Beat 2: Ambiguous Consequential Scope ("Clear my afternoon")

```json
{"timestamp": "2026-10-02T00:20:10.110Z", "component": "intent_parser", "event": "INTENT_EXTRACTED", "goal": "Clear afternoon calendar", "scope": "calendar", "ambiguities": ["unspecified_treatment_of_external_attendees"]}
{"timestamp": "2026-10-02T00:20:10.510Z", "component": "planner_dry_run", "event": "TARGETS_RESOLVED", "affected_events": ["evt_1pm_sync", "evt_2pm_1on1", "evt_4pm_client_vip"]}
{"timestamp": "2026-10-02T00:20:10.550Z", "component": "planner_dry_run", "event": "EXTERNAL_PARTY_FLAGGED", "event_id": "evt_4pm_client_vip", "external_email": "vp@acme-corp.com"}
{"timestamp": "2026-10-02T00:20:10.590Z", "component": "policy_engine", "event": "EVALUATION_COMPLETE", "outcome": "CLARIFY", "rule": "affects_external_party_without_explicit_scope", "message": "Event 'Q4 Acme Partnership Review' includes external client (vp@acme-corp.com). Confirm whether to cancel or keep."}
```

---

### Beat 3: Consequential Action & RBAC Enforcement (Ticket Closure)

```json
# Run A: Standard User on Escalated Ticket
{"timestamp": "2026-10-02T00:25:01.010Z", "actor": "user_mithun", "user_role": "STANDARD_USER", "action": "tickets.update_status", "resource_id": "tkt_402"}
{"timestamp": "2026-10-02T00:25:01.040Z", "component": "policy_engine", "event": "TAG_EVALUATED", "tag": "escalation", "priority": "P1"}
{"timestamp": "2026-10-02T00:25:01.060Z", "component": "policy_engine", "event": "EVALUATION_COMPLETE", "outcome": "CONFIRM", "message": "Ticket #402 is an active P1 Escalation. Explicit confirmation required before closure."}

# Run B: READ_ONLY User Attempting Same Action
{"timestamp": "2026-10-02T00:25:30.010Z", "actor": "user_guest", "user_role": "READ_ONLY", "action": "tickets.update_status", "resource_id": "tkt_402"}
{"timestamp": "2026-10-02T00:25:30.030Z", "component": "policy_engine", "event": "PERMISSION_DENIED", "required": "WRITE_TICKETS", "actual": "READ_ONLY"}
{"timestamp": "2026-10-02T00:25:30.040Z", "component": "policy_engine", "event": "EVALUATION_COMPLETE", "outcome": "BLOCK", "reason": "Role 'READ_ONLY' does not have permission to close tickets."}
```

---

### Beat 4: Partial Failure, Saga Compensation & Drift Detection

```json
{"timestamp": "2026-10-02T00:30:10.100Z", "component": "executor", "event": "BATCH_EXECUTION_STARTED", "total_actions": 2}
{"timestamp": "2026-10-02T00:30:10.250Z", "component": "executor", "event": "ACTION_1_SUCCESS", "action_id": "act_001", "resource_id": "evt_meeting_1"}
{"timestamp": "2026-10-02T00:30:10.280Z", "component": "journal", "event": "LOGGED_DONE", "action_id": "act_001", "after_state": {"time": "14:00"}}

# Step 2 Injected Failure
{"timestamp": "2026-10-02T00:30:10.450Z", "component": "executor", "event": "ACTION_2_FAILED", "action_id": "act_002", "error": "INJECTED_SIMULATED_DATABASE_TIMEOUT"}
{"timestamp": "2026-10-02T00:30:10.470Z", "component": "recovery_engine", "event": "SAGA_COMPENSATION_TRIGGERED", "compensating_actions": ["act_001"]}

# External Drift Occurs: An external user changes evt_meeting_1 before compensation
{"timestamp": "2026-10-02T00:30:10.510Z", "component": "demo_simulator", "event": "EXTERNAL_DRIFT_INJECTED", "resource_id": "evt_meeting_1", "new_time": "15:30"}

# Drift Check Pre-Compensation
{"timestamp": "2026-10-02T00:30:10.550Z", "component": "drift_detector", "event": "DRIFT_CHECK", "resource_id": "evt_meeting_1", "expected_after_state": "14:00", "current_live_state": "15:30"}
{"timestamp": "2026-10-02T00:30:10.570Z", "component": "drift_detector", "event": "DRIFT_DETECTED", "resource_id": "evt_meeting_1", "decision": "HALT_COMPENSATION"}
{"timestamp": "2026-10-02T00:30:10.590Z", "component": "journal", "event": "STATUS_UPDATED", "action_id": "act_001", "status": "drift"}
{"timestamp": "2026-10-02T00:30:10.610Z", "component": "incident_logger", "event": "INCIDENT_LOGGED", "incident_id": "inc_901", "alert": "Resource 'evt_meeting_1' was modified externally after agent execution. Rollback aborted to protect human edits. Human review required."}
```
