import streamlit as st
import json
import uuid
import sys
import os
from pathlib import Path
from datetime import datetime, timezone

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.intent.parser import IntentParser
from src.intent.providers.gemini_provider import GeminiProvider
from src.mcp.domains.calendar_module import CalendarDomainStore
from src.mcp.domains.tickets_module import TicketsDomainStore
from src.mcp.server import CareMCPServer
from src.planner.planner import DryRunPlanner
from src.policy.engine import PolicyEngine
from src.journal.db import ActionJournalDB
from src.executor.runner import ControlledExecutor
from src.schemas.plan import PlanStatus, PlannedAction, CompensationAction, CandidatePlan, RiskLevel, PolicyOutcome
from src.schemas.intent import StructuredIntent
from src.agent.care_agent import CareAgent, AgentResponse

# =============================================================================
# Page Config
# =============================================================================
st.set_page_config(
    page_title="CARE Agent",
    layout="wide",
    initial_sidebar_state="expanded",
)

# =============================================================================
# CSS — @import for fonts (the ONLY way Streamlit allows external fonts)
# =============================================================================
st.markdown("""<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');

/* ===== ROOT THEME ===== */
:root {
    --bg-primary: #0a0d13;
    --bg-secondary: #0f1219;
    --bg-card: #12161f;
    --bg-hover: #181d28;
    --border: rgba(255,255,255,0.06);
    --border-hover: rgba(99,102,241,0.3);
    --text-primary: #e2e6f0;
    --text-secondary: #8892a8;
    --text-muted: #4e5670;
    --accent: #6366f1;
    --accent-light: #818cf8;
    --green: #10b981;
    --yellow: #f59e0b;
    --red: #ef4444;
    --radius: 14px;
    --radius-sm: 10px;
    --font: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    --mono: 'JetBrains Mono', 'SF Mono', 'Fira Code', monospace;
}

/* ===== GLOBAL ===== */
html, body, [class*="css"], .stApp,
[data-testid="stAppViewContainer"],
[data-testid="stApp"] {
    font-family: var(--font) !important;
    background-color: var(--bg-primary) !important;
    color: var(--text-primary) !important;
}

/* ===== HIDE CHROME ===== */
#MainMenu, footer,
[data-testid="stHeader"],
[data-testid="stDecoration"] {
    display: none !important;
}

/* ===== MAIN CONTENT ===== */
[data-testid="stAppViewBlockContainer"],
.block-container {
    max-width: 880px !important;
    padding: 2rem 1rem 6rem 1rem !important;
}

/* ===== SIDEBAR ===== */
[data-testid="stSidebar"] {
    background: var(--bg-secondary) !important;
    border-right: 1px solid var(--border) !important;
}
[data-testid="stSidebar"] [data-testid="stMarkdown"] p,
[data-testid="stSidebar"] [data-testid="stMarkdown"] span,
[data-testid="stSidebar"] [data-testid="stCaptionContainer"] p,
[data-testid="stSidebar"] label {
    font-family: var(--font) !important;
    color: var(--text-secondary) !important;
}
[data-testid="stSidebar"] h3 {
    font-family: var(--font) !important;
    color: var(--text-primary) !important;
    font-weight: 700 !important;
    letter-spacing: -0.02em !important;
}

/* Sidebar section labels */
.sb-label {
    font-family: var(--font) !important;
    font-size: 0.66rem !important;
    font-weight: 600 !important;
    text-transform: uppercase !important;
    letter-spacing: 0.1em !important;
    color: var(--text-muted) !important;
    margin: 1.5rem 0 0.5rem 0 !important;
    padding-bottom: 6px !important;
    border-bottom: 1px solid var(--border) !important;
    display: block !important;
}

/* Sidebar buttons */
[data-testid="stSidebar"] [data-testid="stBaseButton-secondary"] {
    background: var(--bg-card) !important;
    border: 1px solid var(--border) !important;
    border-radius: var(--radius-sm) !important;
    color: var(--text-secondary) !important;
    font-family: var(--font) !important;
    font-size: 0.82rem !important;
    font-weight: 500 !important;
    padding: 0.5rem 0.85rem !important;
    transition: all 0.22s ease !important;
    text-align: left !important;
}
[data-testid="stSidebar"] [data-testid="stBaseButton-secondary"]:hover {
    background: var(--bg-hover) !important;
    border-color: var(--border-hover) !important;
    color: var(--text-primary) !important;
    transform: translateY(-1px) !important;
    box-shadow: 0 4px 12px rgba(0,0,0,0.2) !important;
}

/* ===== INPUTS (text, select, password) ===== */
[data-testid="stTextInput"] input,
[data-testid="stSelectbox"] > div > div {
    background: var(--bg-card) !important;
    border: 1px solid var(--border) !important;
    border-radius: var(--radius-sm) !important;
    color: var(--text-primary) !important;
    font-family: var(--font) !important;
    font-size: 0.85rem !important;
    transition: border-color 0.2s ease, box-shadow 0.2s ease !important;
}
[data-testid="stTextInput"] input:focus {
    border-color: var(--border-hover) !important;
    box-shadow: 0 0 0 3px rgba(99,102,241,0.1) !important;
    outline: none !important;
}

/* ===== CHAT MESSAGES ===== */
[data-testid="stChatMessage"] {
    background: var(--bg-card) !important;
    border: 1px solid var(--border) !important;
    border-radius: var(--radius) !important;
    padding: 1.1rem 1.3rem !important;
    margin-bottom: 0.85rem !important;
    animation: fadeSlideIn 0.4s cubic-bezier(0.16, 1, 0.3, 1) both !important;
    transition: border-color 0.2s ease !important;
}
[data-testid="stChatMessage"]:hover {
    border-color: rgba(255,255,255,0.08) !important;
}
@keyframes fadeSlideIn {
    from { opacity: 0; transform: translateY(12px); }
    to   { opacity: 1; transform: translateY(0); }
}
[data-testid="stChatMessage"] p,
[data-testid="stChatMessage"] li {
    font-family: var(--font) !important;
    font-size: 0.88rem !important;
    line-height: 1.7 !important;
    color: var(--text-secondary) !important;
}
[data-testid="stChatMessage"] strong {
    color: var(--text-primary) !important;
    font-weight: 600 !important;
}
[data-testid="stChatMessage"] code {
    font-family: var(--mono) !important;
    background: rgba(99,102,241,0.1) !important;
    color: var(--accent-light) !important;
    padding: 2px 7px !important;
    border-radius: 6px !important;
    font-size: 0.8rem !important;
    border: 1px solid rgba(99,102,241,0.12) !important;
}

/* Chat avatars */
[data-testid="stChatMessage"] [data-testid="stChatMessageAvatarUser"],
[data-testid="stChatMessage"] [data-testid="stChatMessageAvatarAssistant"] {
    border-radius: var(--radius-sm) !important;
}

/* ===== CHAT INPUT ===== */
[data-testid="stChatInput"] {
    background: var(--bg-secondary) !important;
    border-top: 1px solid var(--border) !important;
}
[data-testid="stChatInput"] textarea,
[data-testid="stChatInputTextArea"] {
    background: var(--bg-card) !important;
    border: 1px solid var(--border) !important;
    border-radius: var(--radius) !important;
    color: var(--text-primary) !important;
    font-family: var(--font) !important;
    font-size: 0.88rem !important;
    padding: 0.75rem 1rem !important;
    transition: border-color 0.25s ease, box-shadow 0.25s ease !important;
}
[data-testid="stChatInput"] textarea:focus,
[data-testid="stChatInputTextArea"]:focus {
    border-color: var(--accent) !important;
    box-shadow: 0 0 0 3px rgba(99,102,241,0.12) !important;
    outline: none !important;
}

/* ===== PRIMARY BUTTON ===== */
[data-testid="stBaseButton-primary"] {
    background: linear-gradient(135deg, var(--accent), var(--accent-light)) !important;
    border: none !important;
    border-radius: var(--radius-sm) !important;
    color: white !important;
    font-family: var(--font) !important;
    font-weight: 600 !important;
    font-size: 0.82rem !important;
    padding: 0.55rem 1.25rem !important;
    transition: all 0.25s cubic-bezier(0.4, 0, 0.2, 1) !important;
    box-shadow: 0 2px 8px rgba(99,102,241,0.3) !important;
}
[data-testid="stBaseButton-primary"]:hover {
    transform: translateY(-1px) !important;
    box-shadow: 0 6px 20px rgba(99,102,241,0.4) !important;
}
[data-testid="stBaseButton-primary"]:active {
    transform: translateY(0) !important;
}

/* ===== SECONDARY BUTTON (main area) ===== */
[data-testid="stBaseButton-secondary"] {
    background: var(--bg-card) !important;
    border: 1px solid var(--border) !important;
    border-radius: var(--radius-sm) !important;
    color: var(--text-secondary) !important;
    font-family: var(--font) !important;
    font-size: 0.82rem !important;
    font-weight: 500 !important;
    transition: all 0.22s ease !important;
}
[data-testid="stBaseButton-secondary"]:hover {
    background: var(--bg-hover) !important;
    border-color: var(--border-hover) !important;
    color: var(--text-primary) !important;
    transform: translateY(-1px) !important;
}

/* ===== EXPANDER ===== */
[data-testid="stExpander"] {
    border: 1px solid var(--border) !important;
    border-radius: var(--radius-sm) !important;
    overflow: hidden !important;
    transition: border-color 0.2s ease !important;
}
[data-testid="stExpander"]:hover {
    border-color: rgba(255,255,255,0.08) !important;
}
[data-testid="stExpander"] summary,
[data-testid="stExpander"] [data-testid="stExpanderToggleDetails"] {
    background: var(--bg-card) !important;
    font-family: var(--font) !important;
    font-size: 0.82rem !important;
    font-weight: 500 !important;
    color: var(--text-muted) !important;
    border: none !important;
    padding: 0.7rem 1rem !important;
    transition: color 0.2s ease !important;
}
[data-testid="stExpander"] summary:hover,
[data-testid="stExpander"] [data-testid="stExpanderToggleDetails"]:hover {
    color: var(--text-secondary) !important;
}
[data-testid="stExpander"] [data-testid="stExpanderDetails"] {
    background: rgba(10,13,19,0.5) !important;
    padding: 0.75rem 1rem !important;
}

/* ===== TOP NAV BAR (custom HTML) ===== */
.care-nav {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0.9rem 1.2rem;
    margin-bottom: 1.5rem;
    background: linear-gradient(135deg, rgba(18,22,31,0.9), rgba(15,18,25,0.95));
    border: 1px solid var(--border);
    border-radius: 16px;
    backdrop-filter: blur(16px);
    -webkit-backdrop-filter: blur(16px);
}
.care-nav-left {
    display: flex;
    align-items: center;
    gap: 14px;
}
.care-logo {
    width: 36px; height: 36px;
    background: linear-gradient(135deg, #6366f1, #a78bfa);
    border-radius: 10px;
    display: flex; align-items: center; justify-content: center;
    font-family: var(--font);
    font-weight: 700; font-size: 1rem; color: #fff;
    box-shadow: 0 2px 10px rgba(99,102,241,0.25);
}
.care-nav-title {
    font-family: var(--font);
    font-size: 1rem; font-weight: 700;
    color: var(--text-primary);
    letter-spacing: -0.01em;
    line-height: 1.2;
}
.care-nav-sub {
    font-family: var(--font);
    font-size: 0.75rem;
    color: var(--text-muted);
    font-weight: 400;
}
.care-status {
    display: inline-flex; align-items: center; gap: 7px;
    padding: 4px 12px;
    border-radius: 999px;
    background: rgba(16,185,129,0.08);
    border: 1px solid rgba(16,185,129,0.15);
    font-family: var(--font);
    font-size: 0.7rem; font-weight: 500; color: #34d399;
}
.care-dot {
    width: 7px; height: 7px;
    border-radius: 50%;
    background: #10b981;
    animation: dotPulse 2.5s ease-in-out infinite;
}
@keyframes dotPulse {
    0%, 100% { opacity: 1; box-shadow: 0 0 0 0 rgba(16,185,129,0.4); }
    50% { opacity: 0.6; box-shadow: 0 0 0 5px rgba(16,185,129,0); }
}

/* ===== TRACE SECTION HEADING ===== */
.tr-heading {
    font-family: var(--font) !important;
    font-size: 0.65rem !important;
    font-weight: 600 !important;
    text-transform: uppercase !important;
    letter-spacing: 0.09em !important;
    color: var(--text-muted) !important;
    padding-bottom: 0.4rem !important;
    margin-bottom: 0.4rem !important;
    border-bottom: 1px solid var(--border) !important;
    display: block !important;
}

/* ===== POLICY PILLS ===== */
.pl {
    display: inline-flex; align-items: center; gap: 5px;
    padding: 3px 10px; border-radius: 8px;
    font-family: var(--mono) !important;
    font-size: 0.7rem; font-weight: 600; letter-spacing: 0.02em;
}
.pl-ok  { background: rgba(16,185,129,0.1); border: 1px solid rgba(16,185,129,0.2); color: #6ee7b7; }
.pl-cl  { background: rgba(245,158,11,0.1); border: 1px solid rgba(245,158,11,0.2); color: #fcd34d; }
.pl-cf  { background: rgba(129,140,248,0.1); border: 1px solid rgba(129,140,248,0.2); color: #a5b4fc; }
.pl-bk  { background: rgba(239,68,68,0.1); border: 1px solid rgba(239,68,68,0.2); color: #fca5a5; }

/* ===== ALERTS ===== */
[data-testid="stAlert"] {
    border-radius: var(--radius-sm) !important;
    font-family: var(--font) !important;
    font-size: 0.82rem !important;
}

/* ===== SCROLLBAR ===== */
::-webkit-scrollbar { width: 6px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.07); border-radius: 999px; }
::-webkit-scrollbar-thumb:hover { background: rgba(255,255,255,0.14); }

/* ===== DIVIDER ===== */
hr {
    border: none !important;
    border-top: 1px solid var(--border) !important;
    margin: 0.75rem 0 !important;
}
</style>""", unsafe_allow_html=True)


# =============================================================================
# Session State Init
# =============================================================================
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
    st.session_state.parser = IntentParser(use_cache=True, fallback_on_error=True)
    st.session_state.planner = DryRunPlanner(mcp_server=st.session_state.mcp_server)
    st.session_state.policy_engine = PolicyEngine(mcp_server=st.session_state.mcp_server)
    st.session_state.agent = CareAgent(
        mcp_server=st.session_state.mcp_server,
        journal_db=st.session_state.journal_db,
        executor=st.session_state.executor,
        parser=st.session_state.parser,
        planner=st.session_state.planner,
        policy_engine=st.session_state.policy_engine,
    )
    st.session_state.messages = [
        {
            "role": "assistant",
            "content": (
                "**CARE Agent Ready**\n\n"
                "Coordinate calendar events, manage tickets, and automate workflows "
                "with deterministic safety. Every action passes through policy gating, "
                "plan verification, and journaled execution before committing.\n\n"
                "Type a command below, or pick a preset from the sidebar."
            ),
            "trace": None,
        }
    ]
    st.session_state.initialized = True


def reset_all_stores():
    st.session_state.calendar_store.reset()
    st.session_state.tickets_store.reset()
    st.session_state.messages = [
        {
            "role": "assistant",
            "content": "Environment reset to seed data. Ready for commands.",
            "trace": None,
        }
    ]
    st.session_state.agent.pending_plan = None


# =============================================================================
# Sidebar
# =============================================================================
with st.sidebar:
    st.markdown("### CARE")
    st.caption("Context-Aware Reasoning & Execution")

    st.markdown('<span class="sb-label">Inference Provider</span>', unsafe_allow_html=True)

    current_key = os.getenv("GEMINI_API_KEY", "")
    api_key_input = st.text_input(
        "Gemini API Key",
        value=st.session_state.get("gemini_api_key", current_key),
        type="password",
        help="Paste your Gemini key for live LLM parsing.",
    )
    if api_key_input and api_key_input != st.session_state.get("gemini_api_key"):
        st.session_state.gemini_api_key = api_key_input
        try:
            gemini_provider = GeminiProvider(api_key=api_key_input)
            st.session_state.parser = IntentParser(provider=gemini_provider, fallback_on_error=True)
            st.session_state.agent.parser = st.session_state.parser
            st.success("Gemini connected")
        except Exception as e:
            st.error(f"Connection failed: {e}")

    p_status = st.session_state.parser.status
    if p_status["is_live"]:
        st.caption(f"Gemini Live ({getattr(st.session_state.parser.provider, 'model', 'gemini-2.5-flash')})")
    else:
        st.caption("Offline deterministic engine")

    st.markdown('<span class="sb-label">Access Control</span>', unsafe_allow_html=True)
    user_role = st.selectbox(
        "Role",
        ["STANDARD_USER", "ADMIN", "READ_ONLY"],
        index=0,
        help="Deterministic RBAC boundary enforced by the policy engine.",
    )
    actor_id = st.text_input(
        "Actor",
        value="user_mithun" if user_role != "READ_ONLY" else "user_intern",
    )

    st.markdown('<span class="sb-label">Preset Commands</span>', unsafe_allow_html=True)

    quick_prompts = [
        ("View Schedule", "What meetings do I have today?"),
        ("Book Meeting", "Schedule meeting with Alice at 4 PM"),
        ("Reschedule 3 PM", "Move my 3 PM meeting to 4 PM"),
        ("Cancel Meeting", "Cancel my 10 AM meeting"),
        ("Clear Afternoon", "Clear my afternoon so I can finish the proposal"),
        ("Open Tickets", "What tickets are open?"),
        ("Escalation #402", "Close escalated ticket #402"),
        ("File Incident", "Create ticket for memory leak in auth worker"),
        ("About CARE", "Explain how CARE prevents unauthorized state changes"),
    ]

    for label, prompt_text in quick_prompts:
        if st.button(label, use_container_width=True):
            st.session_state.messages.append({"role": "user", "content": prompt_text, "trace": None})
            response: AgentResponse = st.session_state.agent.process_message(
                prompt_text, user_role=user_role, actor_id=actor_id
            )
            st.session_state.messages.append({
                "role": "assistant",
                "content": response.text,
                "trace": response,
            })
            st.rerun()

    st.markdown('<span class="sb-label">Live State</span>', unsafe_allow_html=True)
    with st.expander("Domain Stores", expanded=False):
        st.markdown("**Calendar**")
        for ev in st.session_state.calendar_store.events.values():
            has_ext = any(att.get("is_external", False) for att in ev.get("attendees", []))
            tag = "External" if has_ext else "Internal"
            st.caption(f"`{ev['id']}` {ev['start_time'][11:16]} — {ev['title']}  [{tag}]")

        st.markdown("**Tickets**")
        for tkt in st.session_state.tickets_store.tickets.values():
            esc = " [Escalated]" if tkt.get("is_escalated") else ""
            st.caption(f"`{tkt['id']}` {tkt['title']} [{tkt['status'].upper()}]{esc}")

    if st.button("Reset Environment", use_container_width=True):
        reset_all_stores()
        st.rerun()


# =============================================================================
# Top Nav Bar
# =============================================================================
st.markdown("""
<div class="care-nav">
    <div class="care-nav-left">
        <div class="care-logo">C</div>
        <div>
            <div class="care-nav-title">CARE Agent</div>
            <div class="care-nav-sub">Context-Aware Reasoning & Execution</div>
        </div>
    </div>
    <div class="care-status">
        <span class="care-dot"></span>
        Active
    </div>
</div>
""", unsafe_allow_html=True)


# =============================================================================
# Trace Renderer
# =============================================================================
def render_care_trace(response: AgentResponse):
    if not response.plan and not response.intent:
        return

    with st.expander("View pipeline trace", expanded=False):
        col1, col2, col3 = st.columns(3)

        with col1:
            st.markdown('<span class="tr-heading">Structured Intent</span>', unsafe_allow_html=True)
            if response.intent:
                st.caption(f"Goal: `{response.intent.goal}`")
                st.caption(f"Scope: `{response.intent.scope}`")
                ents = ', '.join(response.intent.entities) if response.intent.entities else 'None'
                st.caption(f"Entities: `{ents}`")
                conf = response.intent.intent_confidence.value.upper()
                st.caption(f"Confidence: `{conf}`")
                if response.intent.ambiguities:
                    st.caption(f"Ambiguities: `{', '.join(response.intent.ambiguities)}`")

        with col2:
            st.markdown('<span class="tr-heading">Dry-Run Plan</span>', unsafe_allow_html=True)
            if response.plan:
                st.caption(f"Plan: `{response.plan.plan_id}`")
                st.caption(f"Actions: `{len(response.plan.actions)}`")
                for act in response.plan.actions:
                    st.caption(f"`{act.resource_id}` / `{act.operation}`")
                if response.plan.action_hash:
                    st.caption(f"Hash: `{response.plan.action_hash[:16]}...`")

        with col3:
            st.markdown('<span class="tr-heading">Policy Decision</span>', unsafe_allow_html=True)
            if response.decision:
                outcome = response.decision.outcome
                badge_map = {
                    PolicyOutcome.AUTO_APPROVE: ("pl pl-ok", "AUTO-APPROVE"),
                    PolicyOutcome.CLARIFY:      ("pl pl-cl", "CLARIFY"),
                    PolicyOutcome.CONFIRM:      ("pl pl-cf", "CONFIRM"),
                    PolicyOutcome.BLOCK:        ("pl pl-bk", "BLOCKED"),
                }
                cls, label = badge_map.get(outcome, ("pl", str(outcome)))
                st.markdown(f'<span class="{cls}">{label}</span>', unsafe_allow_html=True)
                st.caption(f"{response.decision.reason}")

        if response.execution_result:
            st.markdown("---")
            status = response.execution_result.get('status', 'unknown')
            records = response.execution_result.get('journal_records', '-')
            st.caption(f"Execution: `{status}` / Journal records: `{records}`")


# =============================================================================
# Chat Loop
# =============================================================================
for msg_idx, message in enumerate(st.session_state.messages):
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

        trace = message.get("trace")
        if trace and isinstance(trace, AgentResponse):
            render_care_trace(trace)

            if trace.needs_confirmation and msg_idx == len(st.session_state.messages) - 1:
                col_c1, col_c2, _ = st.columns([1, 1, 3])
                with col_c1:
                    if st.button("Confirm", key=f"conf_{msg_idx}", type="primary"):
                        st.session_state.messages.append({"role": "user", "content": "Yes, confirm and execute.", "trace": None})
                        res = st.session_state.agent.process_message("yes", user_role=user_role, actor_id=actor_id)
                        st.session_state.messages.append({"role": "assistant", "content": res.text, "trace": res})
                        st.rerun()
                with col_c2:
                    if st.button("Cancel", key=f"canc_{msg_idx}"):
                        st.session_state.messages.append({"role": "user", "content": "No, cancel.", "trace": None})
                        res = st.session_state.agent.process_message("cancel", user_role=user_role, actor_id=actor_id)
                        st.session_state.messages.append({"role": "assistant", "content": res.text, "trace": res})
                        st.rerun()

            elif trace.suggested_actions and msg_idx == len(st.session_state.messages) - 1:
                sug_cols = st.columns(min(len(trace.suggested_actions), 3))
                for s_idx, action_text in enumerate(trace.suggested_actions[:3]):
                    with sug_cols[s_idx]:
                        if st.button(action_text, key=f"sug_{msg_idx}_{s_idx}", use_container_width=True):
                            st.session_state.messages.append({"role": "user", "content": action_text, "trace": None})
                            res = st.session_state.agent.process_message(action_text, user_role=user_role, actor_id=actor_id)
                            st.session_state.messages.append({"role": "assistant", "content": res.text, "trace": res})
                            st.rerun()


# =============================================================================
# Chat Input
# =============================================================================
user_input = st.chat_input("Type a command (e.g. 'Schedule meeting at 4 PM', 'Cancel 10 AM', 'Close ticket #402')")

if user_input:
    st.session_state.messages.append({"role": "user", "content": user_input, "trace": None})
    response: AgentResponse = st.session_state.agent.process_message(
        user_input, user_role=user_role, actor_id=actor_id
    )
    st.session_state.messages.append({
        "role": "assistant",
        "content": response.text,
        "trace": response,
    })
    st.rerun()
