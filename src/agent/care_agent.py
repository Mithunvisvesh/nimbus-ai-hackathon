import re
import json
import uuid
import logging
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field

from src.schemas.intent import StructuredIntent, IntentConfidence
from src.schemas.plan import (
    CandidatePlan,
    PlannedAction,
    CompensationAction,
    RiskLevel,
    PlanStatus,
    PolicyOutcome,
)
from src.policy.engine import PolicyEngine, PolicyEvaluationResult
from src.planner.planner import DryRunPlanner
from src.executor.runner import ControlledExecutor
from src.mcp.server import CareMCPServer
from src.journal.db import ActionJournalDB
from src.verifier.invariant_checker import InvariantChecker
from src.intent.parser import IntentParser

logger = logging.getLogger(__name__)


@dataclass
class AgentResponse:
    """Structured response from CARE Agent for UI/chat rendering."""
    text: str
    intent: Optional[StructuredIntent] = None
    plan: Optional[CandidatePlan] = None
    decision: Optional[PolicyEvaluationResult] = None
    execution_result: Optional[Dict[str, Any]] = None
    needs_confirmation: bool = False
    needs_clarification: bool = False
    suggested_actions: List[str] = field(default_factory=list)
    raw_query_data: Optional[Any] = None


class CareAgent:
    """
    General-purpose Conversational AI Agent powered by the CARE Framework.
    
    Acts like a true intelligent assistant:
    1. Handles natural conversations, queries, and instructions.
    2. Understands read-only queries (schedule, tickets, availability) and answers informatively.
    3. Translates state-changing tasks into StructuredIntent -> Dry-Run Plan -> Deterministic Policy Gate.
    4. Automatically executes verified, safe actions through FastMCP & Write-Ahead Journal.
    5. Prompts for human confirmation or clarification when policy requires it.
    """

    def __init__(
        self,
        mcp_server: CareMCPServer,
        journal_db: ActionJournalDB,
        executor: ControlledExecutor,
        parser: IntentParser,
        planner: DryRunPlanner,
        policy_engine: PolicyEngine,
    ):
        self.mcp = mcp_server
        self.journal = journal_db
        self.executor = executor
        self.parser = parser
        self.planner = planner
        self.policy = policy_engine
        self.verifier = InvariantChecker()
        self.pending_plan: Optional[CandidatePlan] = None

    def process_message(
        self,
        user_message: str,
        user_role: str = "STANDARD_USER",
        actor_id: str = "user_mithun",
    ) -> AgentResponse:
        cleaned = user_message.strip()
        lower = cleaned.lower()

        # -------------------------------------------------------------
        # 1. Handle Pending Confirmation (e.g. user says "yes", "confirm", "proceed")
        # -------------------------------------------------------------
        if self.pending_plan is not None and any(w in lower for w in ["yes", "confirm", "proceed", "go ahead", "do it"]):
            plan = self.pending_plan
            self.pending_plan = None
            try:
                plan.policy_outcome = PolicyOutcome.CONFIRM
                plan.status = PlanStatus.APPROVED
                self.journal.save_approved_plan(plan, explicit_confirmation=True)
                exec_result = self.executor.execute_plan(plan.plan_id, plan.action_hash)
                
                # Format friendly confirmation reply
                targets = ", ".join(f"`{act.resource_id}`" for act in plan.actions)
                reply = f"**Confirmed and executed.** The approved actions on {targets} have been committed via FastMCP and logged to the write-ahead journal."
                return AgentResponse(
                    text=reply,
                    intent=plan.intent,
                    plan=plan,
                    decision=PolicyEvaluationResult(PolicyOutcome.CONFIRM, "User provided explicit confirmation."),
                    execution_result=exec_result,
                )
            except Exception as e:
                logger.error(f"Execution error on confirmed plan: {e}")
                return AgentResponse(text=f"Execution failed: {str(e)}")

        # If user cancels pending plan
        if self.pending_plan is not None and any(w in lower for w in ["no", "cancel", "stop", "abort"]):
            self.pending_plan = None
            return AgentResponse(text="Understood. The pending action has been cancelled without modifying any system state.")

        # -------------------------------------------------------------
        # 2. Read Queries (Calendar & Tickets)
        # -------------------------------------------------------------
        # 2a. Calendar List / Check
        if any(w in lower for w in ["what", "list", "show", "check", "see", "view"]) and any(
            w in lower for w in ["meeting", "calendar", "schedule", "event", "agenda"]
        ):
            events = self.mcp.list_calendar_events()
            if not events:
                return AgentResponse(text="Your calendar is completely clear! No events scheduled.")
            
            lines = ["**Current schedule:**\n"]
            for ev in events:
                attendees = ev.get("attendees", [])
                has_external = any(att.get("is_external", False) for att in attendees)
                ext_badge = " *(External)*" if has_external else " *(Internal)*"
                att_names = ", ".join(att.get("name", "Someone") for att in attendees)
                lines.append(f"• **{ev.get('start_time', '')[11:16]} - {ev.get('end_time', '')[11:16]}**: `{ev.get('id')}` — **{ev.get('title')}**{ext_badge}\n  Attendees: {att_names}")

            return AgentResponse(
                text="\n".join(lines),
                raw_query_data=events,
                suggested_actions=[
                    "Move my 3 PM meeting to 4 PM",
                    "Clear my afternoon so I can finish the proposal",
                ]
            )

        # 2b. Tickets List
        if any(w in lower for w in ["what", "list", "show", "check", "see", "view"]) and any(
            w in lower for w in ["ticket", "tkt", "issue", "bug"]
        ):
            tickets = self.mcp.list_tickets() if hasattr(self.mcp, "list_tickets") else []
            if not tickets:
                return AgentResponse(text="No tickets found in the system.")
            
            lines = ["**Active tickets:**\n"]
            for tkt in tickets:
                esc_badge = " **[Escalated]**" if tkt.get("is_escalated") else ""
                lines.append(f"• `{tkt['id']}`: **{tkt.get('title')}** [{tkt.get('status').upper()}]{esc_badge}\n  Priority: `{tkt.get('priority')}` | Assignee: `{tkt.get('assigned_to')}`")

            return AgentResponse(
                text="\n".join(lines),
                raw_query_data=tickets,
                suggested_actions=[
                    "Close escalated ticket #402",
                    "Close ticket #105",
                ]
            )

        # -------------------------------------------------------------
        # 3. Probabilistic Intent Parsing
        # -------------------------------------------------------------
        intent = self.parser.parse(cleaned, user_id=actor_id)

        # If the intent is purely conversational / general question (not calendar or tickets)
        if intent.scope == "general":
            action_clues = any(w in lower for w in ["schedule", "reschedule", "book", "meeting", "ticket", "cancel", "tkt", "event"])
            if action_clues:
                reply_text = (
                    f"I understand you want to coordinate a meeting or ticket (**\"{cleaned}\"**), "
                    f"but I need a few more details to formulate a verified plan.\n\n"
                    f"Could you specify the target time or subject (e.g., *\"Schedule client meeting for 4 PM today\"* or *\"Move my 3 PM meeting to 4 PM\"*)?"
                )
            else:
                reply_text = self._answer_conversational(cleaned)
            return AgentResponse(
                text=reply_text,
                intent=intent,
                needs_clarification=action_clues,
                suggested_actions=[
                    "What meetings do I have today?",
                    "Schedule a meeting with Alice tomorrow at 4 PM",
                    "Move my 3 PM meeting to 4 PM",
                    "Close escalated ticket #402",
                ]
            )

        # -------------------------------------------------------------
        # 4. State-Changing Action Workflow (The CARE Pipeline)
        # -------------------------------------------------------------
        # Step 1: Read-Only Dry Run & Target Resolution
        plan = self.planner.generate_candidate_plan(intent=intent, actor=actor_id, user_role=user_role)

        # Step 2: Handle Unresolved Target Resources Gracefully
        if not plan.actions:
            if intent.scope == "calendar":
                events = self.mcp.list_calendar_events()
                lines = [f"I searched your calendar for an event matching **\"{cleaned}\"**, but couldn't locate a match.\n"]
                if events:
                    lines.append("**Here is your current schedule:**")
                    for ev in events:
                        lines.append(f"• `{ev.get('id')}` ({ev.get('start_time', '')[11:16]}): **{ev.get('title')}**")
                    suggestions = [f"Move {ev.get('title')[:15]} to 4 PM" for ev in events[:2]]
                else:
                    lines.append("Your calendar is currently clear.")
                    suggestions = ["Schedule a meeting with Alice at 4 PM"]
                return AgentResponse(
                    text="\n".join(lines),
                    intent=intent,
                    plan=plan,
                    needs_clarification=True,
                    suggested_actions=suggestions,
                )

            elif intent.scope == "tickets":
                tickets = self.mcp.list_tickets() if hasattr(self.mcp, "list_tickets") else []
                lines = [f"I couldn't find a ticket matching **\"{cleaned}\"**.\n"]
                if tickets:
                    lines.append("**Here are the available tickets:**")
                    for tkt in tickets:
                        lines.append(f"• `{tkt.get('id')}`: **{tkt.get('title')}** [{tkt.get('status')}]")
                    suggestions = [f"Close {tkt.get('id')}" for tkt in tickets[:2]]
                else:
                    lines.append("No tickets found in the system.")
                    suggestions = ["Create ticket for payment bug"]
                return AgentResponse(
                    text="\n".join(lines),
                    intent=intent,
                    plan=plan,
                    needs_clarification=True,
                    suggested_actions=suggestions,
                )

        # Step 3: Deterministic Policy & Risk Engine
        decision = self.policy.evaluate(plan)

        # Formulate intelligent natural language response based on policy decision
        if decision.outcome == PolicyOutcome.AUTO_APPROVE:
            # Step 4: Gated Execution & Journaling
            plan.policy_outcome = PolicyOutcome.AUTO_APPROVE
            plan.status = PlanStatus.APPROVED
            self.journal.save_approved_plan(plan)

            try:
                exec_result = self.executor.execute_plan(plan.plan_id, plan.action_hash)
                
                # Format friendly descriptive execution receipt
                act = plan.actions[0]
                if act.operation == "calendar.create_event":
                    reply = (
                        f"**Meeting Scheduled Successfully.**\n\n"
                        f"Created **{act.parameters.get('title')}** (`{act.resource_id}`) from "
                        f"`{act.parameters.get('start_time')[11:16]}` to `{act.parameters.get('end_time')[11:16]}`.\n\n"
                        f"The operation was auto-approved and committed via FastMCP with write-ahead journal verification."
                    )
                elif act.operation == "calendar.update_event" and "[CANCELLED]" in act.parameters.get("title", ""):
                    reply = (
                        f"**Meeting Cancelled Successfully.**\n\n"
                        f"Cancelled `{act.before_state.get('title')}` (`{act.resource_id}`). "
                        f"The action was committed via FastMCP and logged to the write-ahead journal."
                    )
                elif act.operation == "tickets.create_ticket":
                    reply = (
                        f"**Ticket Filed Successfully.**\n\n"
                        f"Created ticket `{act.resource_id}`: **{act.parameters.get('title')}** "
                        f"[Priority: `{act.parameters.get('priority')}`].\n\n"
                        f"Committed to tickets domain store via FastMCP."
                    )
                elif act.operation == "tickets.update_status":
                    reply = (
                        f"**Ticket Status Updated.**\n\n"
                        f"Ticket `{act.resource_id}` status has been updated to **{act.parameters.get('new_status').upper()}**.\n\n"
                        f"Logged to the write-ahead journal and verified against system invariants."
                    )
                else:
                    targets = ", ".join(f"`{a.resource_id}`" for a in plan.actions)
                    reply = (
                        f"**Action completed successfully.**\n\n"
                        f"Executed the requested update on {targets}. "
                        f"The operation was auto-approved because all targets are internal, reversible, and conflict-free."
                    )

                return AgentResponse(
                    text=reply,
                    intent=intent,
                    plan=plan,
                    decision=decision,
                    execution_result=exec_result,
                )
            except Exception as e:
                logger.error(f"Execution error: {e}")
                return AgentResponse(
                    text=f"Execution failed: {str(e)}",
                    intent=intent,
                    plan=plan,
                    decision=decision,
                )

        elif decision.outcome == PolicyOutcome.CLARIFY:
            self.pending_plan = None
            alts = decision.suggested_alternatives
            reply = f"**Clarification Required:** {decision.reason}"
            if alts:
                alt_str = ", ".join(f"`{a[11:16]}`" for a in alts)
                reply += f"\n\nSuggested available slots: {alt_str}."
            
            return AgentResponse(
                text=reply,
                intent=intent,
                plan=plan,
                decision=decision,
                needs_clarification=True,
                suggested_actions=[f"Move to {a[11:16]}" for a in alts] if alts else [
                    "Reschedule only internal catch-ups",
                    "Keep client meeting and reschedule others",
                ],
            )

        elif decision.outcome == PolicyOutcome.CONFIRM:
            self.pending_plan = plan
            reply = (
                f"**Confirmation Required:**\n\n"
                f"{decision.reason}\n\n"
                f"Candidate plan (`{plan.plan_id}`) has been prepared. Because this action has high impact or affects an active escalation, "
                f"the deterministic policy requires explicit authorization before execution."
            )
            return AgentResponse(
                text=reply,
                intent=intent,
                plan=plan,
                decision=decision,
                needs_confirmation=True,
                suggested_actions=["Yes, confirm and execute", "No, cancel this action"],
            )

        elif decision.outcome == PolicyOutcome.BLOCK:
            self.pending_plan = None
            reply = (
                f"**Action Blocked by Deterministic Policy:**\n\n"
                f"{decision.reason}\n\n"
                f"Current role (`{user_role}`) does not possess authorization to perform this operation."
            )
            return AgentResponse(
                text=reply,
                intent=intent,
                plan=plan,
                decision=decision,
            )

        # Fallback
        return AgentResponse(
            text=f"I've analyzed your request: '{intent.goal}'. No concrete actions could be scheduled.",
            intent=intent,
            plan=plan,
            decision=decision,
        )

    def _answer_conversational(self, prompt: str) -> str:
        """Intelligently answers general conversational or assistant inquiries."""
        if self.parser.is_live and hasattr(self.parser.provider, "client"):
            try:
                system_prompt = (
                    "You are the CARE Conversational AI Agent, an intelligent enterprise assistant for calendar coordination, "
                    "ticket management, and task automation. You adhere strictly to the CARE framework "
                    "(Context-Aware Reasoning & Execution). When users ask general questions, seek explanations, or ask for help, "
                    "answer helpfully, clearly, and concisely. Keep responses friendly, transparent, and professional."
                )
                from google.genai import types
                res = self.parser.provider.client.models.generate_content(
                    model=getattr(self.parser.provider, "model", "gemini-2.5-flash"),
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        temperature=0.7,
                    ),
                )
                if res and getattr(res, "text", None):
                    return res.text.strip()
            except Exception as e:
                logger.warning(f"Live conversational query failed: {e}")

        lower = prompt.lower()
        if "care" in lower and any(w in lower for w in ["what", "how", "explain", "why", "architecture", "work", "prevent", "about"]):
            return (
                "**CARE Framework Architecture**\n\n"
                "**CARE** (Context-Aware Reasoning & Execution) couples LLM natural language understanding with deterministic safety gates:\n\n"
                "1. **Probabilistic Intent Extraction**: Converts user input into a strongly typed intent schema without raw execution privileges.\n"
                "2. **Read-Only Dry-Run Planning**: Identifies target entities, validates existence, and records before-state baselines.\n"
                "3. **Deterministic Policy Gating**: Evaluates role permissions, schedule feasibility, external contact policies, and escalation flags.\n"
                "4. **Write-Ahead Journaling**: Logs plan hashes before any tool dispatch for tamper detection.\n"
                "5. **FastMCP Controlled Execution**: Dispatches tool invocations bound to verified plan IDs.\n"
                "6. **Post-Execution Invariant Verification**: Validates expected system state and triggers saga compensation on failure."
            )

        return (
            "**CARE Assistant**\n\n"
            "Enterprise agent for calendar management, ticket resolution, and task automation. "
            "All state modifications are evaluated and approved through the deterministic CARE pipeline before execution.\n\n"
            "**Example commands:**\n"
            "- *\"What meetings do I have today?\"*\n"
            "- *\"Schedule meeting with Alice tomorrow at 4 PM\"*\n"
            "- *\"Move my 3 PM meeting to 4 PM\"*\n"
            "- *\"Cancel my 10 AM meeting\"*\n"
            "- *\"Clear my afternoon so I can finish the proposal\"*\n"
            "- *\"What tickets are open?\"*\n"
            "- *\"Close escalated ticket #402\"*\n"
            "- *\"Create a ticket for database timeout\"*"
        )
