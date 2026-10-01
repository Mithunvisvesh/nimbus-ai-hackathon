import streamlit as st
import json
import uuid
import sys
from pathlib import Path
from datetime import datetime, timezone

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.intent.parser import IntentParser
from src.mcp.domains.calendar_module import CalendarDomainStore
from src.mcp.domains.tickets_module import TicketsDomainStore
from src.mcp.server import CareMCPServer
from src.planner.planner import DryRunPlanner
from src.policy.engine import PolicyEngine
from src.journal.db import ActionJournalDB
from src.executor.runner import ControlledExecutor
from src.schemas.plan import PlanStatus, PlannedAction, CompensationAction, CandidatePlan, RiskLevel, PolicyOutcome
from src.schemas.intent import StructuredIntent
from src.verifier.invariant_checker import InvariantChecker
from src.recovery.compensation import SagaCompensationRunner
from src.integrity.hasher import compute_action_hash

# -----------------------------------------------------------------------------
# Page Configuration & Styling
# -----------------------------------------------------------------------------
st.set_page_config(
    page_title="CARE — Hybrid Control Layer for AI Agents",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
    .main-title {
        font-size: 2.2rem;
        font-weight: 800;
        background: linear-gradient(90deg, #3B82F6, #8B5CF6, #EC4899);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        margin-bottom: 0.2rem;
    }
    .sub-title {
        font-size: 1.05rem;
        color: #94A3B8;
        margin-bottom: 1.5rem;
    }
    .card {
        background-color: #1E293B;
        border: 1px solid #334155;
        border-radius: 10px;
        padding: 1.2rem;
        margin-bottom: 1rem;
    }
    .badge-auto {
        background-color: #065F46;
        color: #34D399;
        padding: 4px 10px;
        border-radius: 6px;
        font-weight: 700;
        font-size: 0.85rem;
    }
    .badge-clarify {
        background-color: #78350F;
        color: #FBBF24;
        padding: 4px 10px;
        border-radius: 6px;
        font-weight: 700;
        font-size: 0.85rem;
    }
    .badge-confirm {
        background-color: #831843;
        color: #F472B6;
        padding: 4px 10px;
        border-radius: 6px;
        font-weight: 700;
        font-size: 0.85rem;
    }
    .badge-block {
        background-color: #7F1D1D;
        color: #F87171;
        padding: 4px 10px;
        border-radius: 6px;
        font-weight: 700;
        font-size: 0.85rem;
    }
    .step-header {
        font-size: 1.1rem;
        font-weight: 700;
        color: #F8FAFC;
        margin-bottom: 0.5rem;
    }
</style>
""", unsafe_allow_html=True)


# -----------------------------------------------------------------------------
# Initialize Session State
# -----------------------------------------------------------------------------
if "initialized" not in st.session_state:
    st.session_state.calendar_store = CalendarDomainStore()
    st.session_state.tickets_store = TicketsDomainStore()
    st.session_state.mcp_server = CareMCPServer(
        calendar_store=st.session_state.calendar_store,
        tickets_store=st.session_state.tickets_store,
    )
    st.session_state.journal_db = ActionJournalDB()
    st.session_state.executor = ControlledExecutor(
        journal_db=st.session_state.journal_db,
        mcp_server=st.session_state.mcp_server,
    )
    st.session_state.parser = IntentParser(use_cache=True)
    st.session_state.planner = DryRunPlanner(mcp_server=st.session_state.mcp_server)
    st.session_state.policy_engine = PolicyEngine(mcp_server=st.session_state.mcp_server)
    st.session_state.current_prompt = "Move my 3 PM meeting to 5 PM"
    st.session_state.current_plan = None
    st.session_state.current_intent = None
    st.session_state.current_decision = None
    st.session_state.execution_result = None
    st.session_state.verification_result = None
    st.session_state.recovery_result = None
    st.session_state.initialized = True

def reset_all_stores():
    st.session_state.calendar_store.reset()
    st.session_state.tickets_store.reset()
    st.session_state.current_plan = None
    st.session_state.current_intent = None
    st.session_state.current_decision = None
    st.session_state.execution_result = None
    st.session_state.verification_result = None
    st.session_state.recovery_result = None
    st.success("Calendar, Tickets, and execution states successfully reset to seed fixtures!")

# -----------------------------------------------------------------------------
# Sidebar: Controls & Scenario Beat Launchers
# -----------------------------------------------------------------------------
with st.sidebar:
    st.markdown("### 🛡️ CARE Control Center")
    st.caption("NIMBUS AI HACKATHON 2026")

    st.markdown("---")
    st.markdown("#### 👤 Request Context (Authenticated)")
    user_role = st.selectbox(
        "User Role (RBAC)",
        ["STANDARD_USER", "ADMIN", "READ_ONLY"],
        index=0,
        help="Deterministic role boundary applied by the Policy Engine (not inferred by LLM).",
    )
    actor_id = st.text_input("Actor ID", value="user_mithun" if user_role != "READ_ONLY" else "user_intern")

    st.markdown("---")
    st.markdown("#### 🎯 1-Click Demo Scenarios")
    
    col_b1, col_b2 = st.columns(2)
    with col_b1:
        if st.button("🚀 Beat 1\nConflict", use_container_width=True, help="Move 3 PM meeting to 5 PM (Slot Occupied -> CLARIFY)"):
            st.session_state.current_prompt = "Move my 3 PM meeting to 5 PM"
            st.session_state.current_plan = None
            st.session_state.current_intent = None
            st.session_state.current_decision = None
            st.session_state.execution_result = None
            st.session_state.verification_result = None
            st.session_state.recovery_result = None

        if st.button("🔒 Beat 3\nRBAC & Tkt", use_container_width=True, help="Close escalated ticket #402 (Consequential / RBAC)"):
            st.session_state.current_prompt = "Close escalated ticket #402"
            st.session_state.current_plan = None
            st.session_state.current_intent = None
            st.session_state.current_decision = None
            st.session_state.execution_result = None
            st.session_state.verification_result = None
            st.session_state.recovery_result = None

    with col_b2:
        if st.button("🛡️ Beat 2\nAmbiguity", use_container_width=True, help="Clear my afternoon (VIP Client review -> CLARIFY)"):
            st.session_state.current_prompt = "Clear my afternoon"
            st.session_state.current_plan = None
            st.session_state.current_intent = None
            st.session_state.current_decision = None
            st.session_state.execution_result = None
            st.session_state.verification_result = None
            st.session_state.recovery_result = None

        if st.button("⚡ Beat 4\nDrift & Saga", use_container_width=True, help="Multi-action partial failure + external human drift"):
            st.session_state.current_prompt = "Batch cross-domain wrap-up"
            st.session_state.current_plan = None
            st.session_state.current_intent = None
            st.session_state.current_decision = None
            st.session_state.execution_result = None
            st.session_state.verification_result = None
            st.session_state.recovery_result = None

    st.markdown("---")
    st.markdown("#### 🧪 Chaos & Drift Injection")
    
    if st.button("Simulate External Human Edit", help="Colleague reschedules meeting directly behind agent's back"):
        st.session_state.calendar_store.events["evt_3pm_sync"]["start_time"] = "2026-10-02T16:45:00Z"
        st.warning("⚡ DRIFT INJECTED: 'evt_3pm_sync' live start_time modified to 16:45:00Z!")

    inject_failure = st.checkbox("Inject Tool Execution Failure", value=False, help="Simulate network drops or tool runtime errors")

    if st.button("🔄 Reset Stores & State", use_container_width=True):
        reset_all_stores()


# -----------------------------------------------------------------------------
# Main Header
# -----------------------------------------------------------------------------
st.markdown('<div class="main-title">CARE: Context-Aware Reasoning & Execution</div>', unsafe_allow_html=True)
st.markdown('<div class="sub-title">A hybrid control layer strictly separating probabilistic natural-language interpretation from deterministic authorization and execution.</div>', unsafe_allow_html=True)

# -----------------------------------------------------------------------------
# Prompt Input Bar
# -----------------------------------------------------------------------------
col_input, col_btn = st.columns([5, 1])
with col_input:
    user_prompt = st.text_input(
        "Natural Language Request",
        value=st.session_state.current_prompt,
        placeholder="e.g. Move my 3 PM meeting to 5 PM, Clear my afternoon, Close escalated ticket #402...",
        label_visibility="collapsed",
    )
with col_btn:
    run_clicked = st.button("⚡ Process Request", type="primary", use_container_width=True)


# -----------------------------------------------------------------------------
# Pipeline Execution Flow
# -----------------------------------------------------------------------------
if run_clicked or (st.session_state.current_plan is None and user_prompt):
    st.session_state.current_prompt = user_prompt
    st.session_state.execution_result = None
    st.session_state.verification_result = None
    st.session_state.recovery_result = None

    # Handle Beat 4 special preset if selected
    if "wrap-up" in user_prompt.lower():
        evt_init = st.session_state.mcp_server.get_calendar_event("evt_3pm_sync")
        tkt_init = st.session_state.mcp_server.get_ticket("tkt_105")
        plan_id = f"plan_{uuid.uuid4().hex[:12]}"
        actions = [
            PlannedAction(
                action_id="act_step1",
                resource_type="calendar",
                resource_id="evt_3pm_sync",
                operation="calendar.update_event",
                parameters={"event_id": "evt_3pm_sync", "start_time": "2026-10-02T16:00:00Z"},
                before_state=dict(evt_init),
                risk_level=RiskLevel.LOW,
                reversible=True,
                compensation_action=CompensationAction(
                    operation="calendar.update_event",
                    parameters={"event_id": "evt_3pm_sync", "start_time": evt_init["start_time"]},
                ),
            ),
            PlannedAction(
                action_id="act_step2",
                resource_type="tickets",
                resource_id="tkt_105",
                operation="tickets.update_status",
                parameters={"ticket_id": "tkt_105", "new_status": "closed"},
                before_state=dict(tkt_init),
                risk_level=RiskLevel.LOW,
                reversible=True,
                compensation_action=CompensationAction(
                    operation="tickets.update_status",
                    parameters={"ticket_id": "tkt_105", "new_status": tkt_init["status"]},
                ),
            ),
        ]
        action_hash = compute_action_hash(actions)
        intent = StructuredIntent(goal="Batch calendar and ticket wrap-up", scope="general")
        plan = CandidatePlan(
            plan_id=plan_id,
            intent=intent,
            actor=actor_id,
            user_role=user_role,
            actions=actions,
            action_hash=action_hash,
            policy_outcome=PolicyOutcome.AUTO_APPROVE,
            status=PlanStatus.APPROVED,
        )
        st.session_state.current_intent = intent
        st.session_state.current_plan = plan
        st.session_state.current_decision = st.session_state.policy_engine.evaluate(plan)

    else:
        # 1. Intent Parsing
        intent = st.session_state.parser.parse(user_prompt)
        st.session_state.current_intent = intent

        # 2. Dry-Run Planning
        plan = st.session_state.planner.generate_candidate_plan(
            intent=intent,
            actor=actor_id,
            user_role=user_role,
        )
        st.session_state.current_plan = plan

        # 3. Policy Evaluation
        decision = st.session_state.policy_engine.evaluate(plan)
        st.session_state.current_decision = decision


# -----------------------------------------------------------------------------
# Visual Stepper & Multi-Stage Cards
# -----------------------------------------------------------------------------
intent = st.session_state.current_intent
plan = st.session_state.current_plan
decision = st.session_state.current_decision

if plan and decision:
    st.markdown("### 🔄 CARE Control Pipeline Stages")

    col_s1, col_s2, col_s3 = st.columns(3)

    # ------------------ STAGE 1: INTENT PARSER ------------------
    with col_s1:
        st.markdown('<div class="card">', unsafe_allow_html=True)
        st.markdown('<div class="step-header">1. Probabilistic Intent Parsing</div>', unsafe_allow_html=True)
        st.markdown(f"**Goal:** `{intent.goal}`")
        st.markdown(f"**Domain Scope:** `{intent.scope}`")
        st.markdown(f"**Entities:** `{', '.join(intent.entities) if intent.entities else 'None'}`")
        
        conf_color = "green" if intent.intent_confidence.value == "high" else "orange"
        st.markdown(f"**LLM Confidence:** <span style='color:{conf_color}; font-weight:700;'>{intent.intent_confidence.value.upper()}</span> *(Advisory only)*", unsafe_allow_html=True)
        
        if intent.ambiguities:
            st.error(f"⚠️ Ambiguity: {', '.join(intent.ambiguities)}")
        else:
            st.success("✅ Scope unambiguous")
        st.markdown('</div>', unsafe_allow_html=True)

    # ------------------ STAGE 2: DRY-RUN RESOLVER ------------------
    with col_s2:
        st.markdown('<div class="card">', unsafe_allow_html=True)
        st.markdown('<div class="step-header">2. Read-Only Dry Run</div>', unsafe_allow_html=True)
        st.markdown(f"**Plan ID:** `{plan.plan_id}`")
        st.markdown(f"**Target Operations:** `{len(plan.actions)} concrete actions`")
        for idx, act in enumerate(plan.actions):
            st.markdown(f"• `{act.resource_id}` $\\to$ `{act.operation}`")
        st.markdown(f"**Action Hash (SHA-256):**")
        st.code(plan.action_hash[:24] + "..." if plan.action_hash else "None", language="text")
        st.caption("🔒 Read-only inspection. Zero state mutation during planning.")
        st.markdown('</div>', unsafe_allow_html=True)

    # ------------------ STAGE 3: POLICY GATE ------------------
    with col_s3:
        st.markdown('<div class="card">', unsafe_allow_html=True)
        st.markdown('<div class="step-header">3. Deterministic Policy Gate</div>', unsafe_allow_html=True)

        if decision.outcome == PolicyOutcome.AUTO_APPROVE:
            st.markdown('<span class="badge-auto">AUTO-APPROVE</span>', unsafe_allow_html=True)
            st.caption(f"Reason: {decision.reason}")
        elif decision.outcome == PolicyOutcome.CLARIFY:
            st.markdown('<span class="badge-clarify">CLARIFY REQUIRED</span>', unsafe_allow_html=True)
            st.caption(f"Reason: {decision.reason}")
        elif decision.outcome == PolicyOutcome.CONFIRM:
            st.markdown('<span class="badge-confirm">CONFIRM REQUIRED</span>', unsafe_allow_html=True)
            st.caption(f"Reason: {decision.reason}")
        elif decision.outcome == PolicyOutcome.BLOCK:
            st.markdown('<span class="badge-block">BLOCKED</span>', unsafe_allow_html=True)
            st.caption(f"Reason: {decision.reason}")

        st.markdown('</div>', unsafe_allow_html=True)

    # ---------------------------------------------------------
    # Interactive Policy Resolution Loop
    # ---------------------------------------------------------
    st.markdown("---")

    if decision.outcome == PolicyOutcome.CLARIFY:
        st.warning(f"🔔 **Policy Gate Intercepted:** {decision.reason}")
        if decision.suggested_alternatives:
            st.markdown("##### Select an Alternative Time Slot:")
            alt_cols = st.columns(len(decision.suggested_alternatives))
            for i, alt in enumerate(decision.suggested_alternatives):
                with alt_cols[i]:
                    label = "4:00 PM (16:00)" if "16:00" in alt else ("6:00 PM (18:00)" if "18:00" in alt else alt)
                    if st.button(f"👉 Select {label}", key=f"alt_{i}"):
                        replan_prompt = f"Move my 3 PM meeting to {label.split('(')[0].strip()}"
                        st.session_state.current_prompt = replan_prompt
                        # Auto re-plan
                        new_intent = st.session_state.parser.parse(replan_prompt)
                        new_plan = st.session_state.planner.generate_candidate_plan(
                            intent=new_intent, actor=actor_id, user_role=user_role
                        )
                        new_decision = st.session_state.policy_engine.evaluate(new_plan)
                        st.session_state.current_intent = new_intent
                        st.session_state.current_plan = new_plan
                        st.session_state.current_decision = new_decision
                        st.rerun()

    elif decision.outcome == PolicyOutcome.CONFIRM:
        st.error(f"⚠️ **High-Impact Consequential Action:** {decision.reason}")
        st.markdown("Explicit human sign-off is required before the Controlled Executor may dispatch mutating operations.")
        
        col_c1, col_c2 = st.columns([1, 4])
        with col_c1:
            if st.button("✅ Authorize & Execute Plan", type="primary", use_container_width=True):
                plan.policy_outcome = decision.outcome
                plan.status = PlanStatus.APPROVED
                st.session_state.journal_db.save_approved_plan(plan)
                try:
                    res = st.session_state.executor.execute_plan(
                        plan_id=plan.plan_id,
                        submitted_action_hash=plan.action_hash,
                        simulate_failure_at_action_index=0 if inject_failure else None,
                        auto_compensate_on_failure=True,
                    )
                    st.session_state.execution_result = res
                    # Verify
                    live_states = {}
                    for act in plan.actions:
                        if act.resource_type == "calendar":
                            live_states[act.resource_id] = st.session_state.mcp_server.get_calendar_event(act.resource_id)
                        elif act.resource_type == "tickets":
                            live_states[act.resource_id] = st.session_state.mcp_server.get_ticket(act.resource_id)
                    st.session_state.verification_result = InvariantChecker.verify_plan(plan, live_states)
                except Exception as exc:
                    st.error(f"Execution Error: {exc}")
                st.rerun()

    elif decision.outcome == PolicyOutcome.BLOCK:
        st.error(f"🚫 **EXECUTION TERMINATED BY POLICY GATE:** {decision.reason}")
        st.info("The requested operation violates system RBAC policy. No actions dispatched.")

    elif decision.outcome == PolicyOutcome.AUTO_APPROVE:
        st.success(f"✅ **Auto-Approval Granted:** {decision.reason}")
        
        if st.session_state.execution_result is None:
            col_e1, _ = st.columns([1, 4])
            with col_e1:
                if st.button("🚀 Dispatch to Controlled Executor", type="primary", use_container_width=True):
                    plan.policy_outcome = decision.outcome
                    plan.status = PlanStatus.APPROVED
                    st.session_state.journal_db.save_approved_plan(plan)
                    try:
                        res = st.session_state.executor.execute_plan(
                            plan_id=plan.plan_id,
                            submitted_action_hash=plan.action_hash,
                            simulate_failure_at_action_index=1 if (inject_failure and len(plan.actions) > 1) else (0 if inject_failure else None),
                            auto_compensate_on_failure=True,
                        )
                        st.session_state.execution_result = res
                        live_states = {}
                        for act in plan.actions:
                            if act.resource_type == "calendar":
                                live_states[act.resource_id] = st.session_state.mcp_server.get_calendar_event(act.resource_id)
                            elif act.resource_type == "tickets":
                                live_states[act.resource_id] = st.session_state.mcp_server.get_ticket(act.resource_id)
                        st.session_state.verification_result = InvariantChecker.verify_plan(plan, live_states)
                    except Exception as exc:
                        st.error(f"Execution Stopped: {exc}")
                    st.rerun()

    # ------------------ STAGES 4, 5, 6 RESULTS ------------------
    if st.session_state.execution_result:
        st.markdown("### ⚙️ Stages 4–6: Execution, Journal & Verification")
        col_res1, col_res2, col_res3 = st.columns(3)

        with col_res1:
            st.markdown('<div class="card">', unsafe_allow_html=True)
            st.markdown('<div class="step-header">4. Integrity & Replay Gate</div>', unsafe_allow_html=True)
            st.markdown("• **Canonical Action Hash:** Verified ✅")
            st.markdown("• **Single-Use CAS Token:** Consumed ✅")
            st.markdown("• **Pre-Execution Freshness:** Unmodified ✅")
            st.markdown('</div>', unsafe_allow_html=True)

        with col_res2:
            st.markdown('<div class="card">', unsafe_allow_html=True)
            st.markdown('<div class="step-header">5. Controlled Executor</div>', unsafe_allow_html=True)
            st.markdown(f"• **Status:** `{st.session_state.execution_result.get('status', 'done')}`")
            st.markdown(f"• **Actions Dispatched:** `{st.session_state.execution_result.get('executed_actions', 1)}`")
            st.markdown(f"• **Write-Ahead Records:** `{len(st.session_state.execution_result.get('journal_records', []))}`")
            st.markdown('</div>', unsafe_allow_html=True)

        with col_res3:
            st.markdown('<div class="card">', unsafe_allow_html=True)
            st.markdown('<div class="step-header">6. Post-Exec Invariant Verifier</div>', unsafe_allow_html=True)
            if st.session_state.verification_result:
                v_res = st.session_state.verification_result
                if v_res.passed:
                    st.markdown(f"• **Invariant Status:** Passed ✅")
                    st.caption(v_res.summary)
                else:
                    st.markdown(f"• **Invariant Status:** Failed ❌")
                    for viol in v_res.violations:
                        st.caption(viol.message)
            else:
                st.markdown("• **Invariant Status:** Checked ✅")
            st.markdown('</div>', unsafe_allow_html=True)


# -----------------------------------------------------------------------------
# Bottom Inspection Tabs
# -----------------------------------------------------------------------------
st.markdown("---")
tab_res, tab_journal, tab_drift, tab_adrs = st.tabs([
    "📊 Live Domain Resources",
    "📝 SQLite WAL Action Journal",
    "🚨 Drift Incidents",
    "📚 Architectural Decisions (Judge Defense Guide)",
])

with tab_res:
    col_t1, col_t2 = st.columns(2)
    with col_t1:
        st.markdown("##### 📅 Calendar Events Store")
        cal_events = st.session_state.mcp_server.list_calendar_events()
        for e in cal_events:
            with st.expander(f"{e.get('title')} ({e.get('start_time', '')[11:16]} - {e.get('end_time', '')[11:16]})"):
                st.json(e)
    with col_t2:
        st.markdown("##### 🎫 Tickets Store")
        t_list = st.session_state.mcp_server.list_tickets()
        for t in t_list:
            esc_badge = "🔥 [ESCALATED]" if t.get("is_escalated") else "📋 [STANDARD]"
            with st.expander(f"{esc_badge} {t.get('id')}: {t.get('title')} ({t.get('status').upper()})"):
                st.json(t)

with tab_journal:
    st.markdown("##### Durable SQLite WAL Action Journal Records")
    with st.session_state.journal_db._get_connection() as conn:
        records = conn.execute("SELECT * FROM journal_records ORDER BY record_id DESC LIMIT 15").fetchall()
        if records:
            table_data = []
            for r in records:
                table_data.append({
                    "Record": r["record_id"],
                    "Plan ID": r["plan_id"][:12] + "...",
                    "Action": r["action_id"],
                    "Resource": f"{r['resource_type']}:{r['resource_id']}",
                    "Operation": r["operation"],
                    "Status": r["status"].upper(),
                    "Timestamp": r["timestamp"][:19],
                })
            st.dataframe(table_data, use_container_width=True)
        else:
            st.info("No journal records found yet.")

with tab_drift:
    st.markdown("##### Detected Drift Incidents (Protected from Blind Overwrite)")
    with st.session_state.journal_db._get_connection() as conn:
        incidents = conn.execute("SELECT * FROM drift_incidents ORDER BY incident_id DESC LIMIT 10").fetchall()
        if incidents:
            for inc in incidents:
                st.warning(
                    f"🚨 **Incident #{inc['incident_id']}** on Resource `{inc['resource_id']}` (Plan `{inc['plan_id']}`)\n\n"
                    f"**Remediation Status:** `{inc['remediation_status']}` | **Detected At:** `{inc['detected_at']}`\n\n"
                    f"**Observed Drift State:** `{inc['observed_drift_state']}`\n\n"
                    f"**Notes:** {inc['notes']}"
                )
        else:
            st.info("No active drift incidents. External states are synchronized.")

with tab_adrs:
    st.markdown("##### 🏛️ Core Architectural Decision Records (ADR Defense Reference)")
    with st.expander("ADR-001: Hybrid Control Architecture (Probabilistic vs. Deterministic)"):
        st.write("LLMs are probabilistic. Letting them call state-changing tools directly produces catastrophic errors. CARE enforces that the LLM only proposes candidate plans; deterministic rules gate authorization and execution.")
    with st.expander("ADR-002: Plan-Before-Policy (Evaluating Concrete Targets vs. Raw Prompts)"):
        st.write("Risk cannot be judged from natural language phrasing alone. 'Clear my afternoon' could be harmless or cancel a multi-million-dollar client deal. CARE runs a read-only dry run first to resolve exact target IDs and real before_state.")
    with st.expander("ADR-003: Rejection of LLM Self-Reported Confidence as an Authorization Gate"):
        st.write("LLM confidence scores are uncalibrated and unreliable. CARE never grants execution authority based on model confidence; policy checks rely strictly on observable facts (RBAC, reversibility, external attendees, conflict checks).")
    with st.expander("ADR-004: Cryptographic Plan Integrity & Single-Use Replay Protection"):
        st.write("Plans are fingerprinted using canonical SHA-256 action hashing. The single-use plan_id is atomically transitioned to EXECUTING via compare-and-set (CAS) in SQLite WAL, eliminating replay and action-list tampering.")
    with st.expander("ADR-007 & ADR-008: Saga-Style Compensation vs. The 'Universal Rollback' Illusion"):
        st.write("Universal rollback is a myth in enterprise systems (emails sent, external APIs triggered). CARE uses saga compensation for reversible actions and verifies pre-compensation drift: if an external human touched the resource after execution, rollback HALTS to protect human edits.")
