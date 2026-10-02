import re
import os
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from src.schemas.intent import (
    StructuredIntent,
    IntentConfidence,
    IntentConstraint,
    ConstraintType,
)
from src.intent.providers.base import (
    LLMProvider,
    LLMProviderError,
    LLMResponseValidationError,
    LLMProviderUnavailableError,
)
from src.intent.providers.gemini_provider import GeminiProvider

logger = logging.getLogger(__name__)

CACHED_RESPONSES_PATH = Path(__file__).resolve().parent.parent.parent / "demo" / "cached_llm_responses.json"


class IntentParser:
    """
    Intent parser interface for CARE.
    Extracts StructuredIntent from:
    1. A real LLM provider (e.g. GeminiProvider) when configured.
    2. Cached fixtures or deterministic fallback patterns when running offline or in demo mode.
    
    The LLM's role is strictly limited to probabilistic natural language interpretation:
        Natural Language -> StructuredIntent
    The LLM never directly executes tools or controls deterministic safety policy.
    """

    def __init__(
        self,
        use_cache: bool = True,
        provider: Optional[LLMProvider] = None,
        mode: Optional[str] = None,
        fallback_on_error: bool = False,
    ):
        """
        Args:
            use_cache: If True and offline, check cached fixture responses before regex rules.
            provider: Explicit LLMProvider instance (e.g. MockLLMProvider or GeminiProvider).
            mode: 'live', 'offline', or 'auto' (default). If 'auto', uses live LLM if API key is present.
            fallback_on_error: If True, falls back to offline cache/rules on live LLM failure.
                               If False (default), re-raises LLMProviderError to keep failures observable.
        """
        self.use_cache = use_cache
        self.fallback_on_error = fallback_on_error
        self.last_source: Optional[str] = None
        self.last_error: Optional[str] = None

        # Resolve mode: explicit argument > env var > 'auto'
        env_mode = os.getenv("CARE_MODE") or os.getenv("LLM_MODE")
        self.mode = (mode or env_mode or "auto").lower()

        # Load cached responses
        self._cached_responses: Dict[str, Any] = {}
        if self.use_cache and CACHED_RESPONSES_PATH.exists():
            try:
                with open(CACHED_RESPONSES_PATH, "r", encoding="utf-8") as f:
                    self._cached_responses = json.load(f)
            except Exception as e:
                logger.warning(f"Could not load cached responses: {e}")
                self._cached_responses = {}

        # Resolve provider
        self.provider: Optional[LLMProvider] = None
        if self.mode == "offline":
            self.provider = None
        elif provider is not None:
            self.provider = provider
        else:
            api_key = os.getenv("GEMINI_API_KEY") or os.getenv("LLM_API_KEY")
            if api_key:
                try:
                    self.provider = GeminiProvider(api_key=api_key)
                except Exception as e:
                    logger.warning(f"Failed to initialize GeminiProvider: {e}")
                    if self.mode == "live":
                        raise LLMProviderUnavailableError(f"Failed to initialize live Gemini provider: {e}") from e
                    self.provider = None
            elif self.mode == "live":
                raise LLMProviderUnavailableError(
                    "Live LLM mode requested but GEMINI_API_KEY/LLM_API_KEY is not configured."
                )

    @property
    def is_live(self) -> bool:
        return self.provider is not None

    @property
    def status(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "is_live": self.is_live,
            "provider": self.provider.__class__.__name__ if self.provider else None,
            "last_source": self.last_source,
            "last_error": self.last_error,
        }

    def parse(self, prompt: str, user_id: Optional[str] = None) -> StructuredIntent:
        normalized_prompt = prompt.strip()

        # 1. Real LLM Provider Path (if available)
        if self.provider is not None:
            try:
                intent = self.provider.extract_intent(normalized_prompt, user_id=user_id)
                self.last_source = "live_llm"
                self.last_error = None
                return intent
            except Exception as e:
                self.last_error = str(e)
                if not self.fallback_on_error:
                    raise
                logger.warning(f"Live LLM provider error ({e}); engaging observable offline fallback.")
                self.last_source = "offline_fallback"
                return self._parse_offline(normalized_prompt)

        # 2. Offline Path (Cached fixture or deterministic rules)
        return self._parse_offline(normalized_prompt)

    def _parse_offline(self, normalized_prompt: str) -> StructuredIntent:
        """Deterministic offline parsing using fixtures or rule patterns."""
        # 1. Check offline cached response if enabled
        if self.use_cache and normalized_prompt in self._cached_responses:
            if self.last_source != "offline_fallback":
                self.last_source = "cache"
            return StructuredIntent.model_validate(self._cached_responses[normalized_prompt])

        # 2. Rule/Pattern fallback (ensures 100% offline hackathon robustness)
        if self.last_source != "offline_fallback":
            self.last_source = "deterministic_rule"

        lower_prompt = normalized_prompt.lower()

        # 2a. Read/Query Patterns
        if any(w in lower_prompt for w in ["what", "list", "show", "check", "see", "view"]) and any(
            w in lower_prompt for w in ["meeting", "calendar", "schedule", "event", "agenda"]
        ):
            return StructuredIntent(
                goal="List calendar events",
                scope="calendar",
                entities=["query_calendar"],
                constraints=[],
                ambiguities=[],
                intent_confidence=IntentConfidence.HIGH,
            )

        if any(w in lower_prompt for w in ["what", "list", "show", "check", "see", "view"]) and any(
            w in lower_prompt for w in ["ticket", "tkt", "issue"]
        ):
            return StructuredIntent(
                goal="List tickets",
                scope="tickets",
                entities=["query_tickets"],
                constraints=[],
                ambiguities=[],
                intent_confidence=IntentConfidence.HIGH,
            )

        # 2b. Calendar Reschedule Pattern (flexible: move, push, reschedule, delay, shift)
        reschedule_match = re.search(
            r"(?:move|push|reschedule|delay|shift)\s+(?:my\s+)?(?:the\s+)?(.+?)\s+to\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)",
            lower_prompt,
        )
        if reschedule_match:
            source_raw = reschedule_match.group(1).strip()
            target_time = reschedule_match.group(2).strip()

            # Check if source_raw contains a clock (e.g. "3 pm meeting" or "3:00 pm")
            clock_in_source = re.search(r"(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)", source_raw)
            if clock_in_source:
                source_time = clock_in_source.group(1).strip()
                entities = [source_time, target_time, "meeting"]
            else:
                source_time = source_raw
                entities = [source_time, target_time, "meeting"]

            date_match = re.search(
                r"\b(20\d{2}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/20\d{2}|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2},?\s+20\d{2})\b",
                lower_prompt,
            )
            if date_match:
                date_text = date_match.group(1)
                parsed_date = None
                for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%B %d %Y", "%b %d %Y"):
                    try:
                        parsed_date = datetime.strptime(date_text.replace(",", ""), fmt).date()
                        break
                    except ValueError:
                        continue
                if parsed_date:
                    entities.append(parsed_date.isoformat())

            ambiguities = []
            if "client" in lower_prompt and "external" not in lower_prompt:
                ambiguities.append("External client attendee implications unconfirmed")

            return StructuredIntent(
                goal=f"Move meeting from {source_time} to {target_time}",
                scope="calendar",
                entities=entities,
                constraints=[
                    IntentConstraint(
                        type=ConstraintType.PRESERVE_ITEMS,
                        params={"items": ["attendees", "location"]},
                        source="inferred",
                    ),
                    IntentConstraint(
                        type=ConstraintType.EXPECTED_STATE_EQUALS_ACTUAL_STATE,
                        params={"property": "start_time"},
                        source="policy",
                    ),
                ],
                ambiguities=ambiguities,
                intent_confidence=IntentConfidence.HIGH if not ambiguities else IntentConfidence.MEDIUM,
            )

        # Calendar "Clear Afternoon" Pattern (Beat 2)
        if "clear" in lower_prompt and ("afternoon" in lower_prompt or "calendar" in lower_prompt or "schedule" in lower_prompt):
            return StructuredIntent(
                goal="Clear afternoon calendar",
                scope="calendar",
                entities=["afternoon"],
                constraints=[
                    IntentConstraint(
                        type=ConstraintType.PRESERVE_ITEMS,
                        params={"property": "external_attendee"},
                        source="inferred",
                    ),
                    IntentConstraint(
                        type=ConstraintType.NO_DESTRUCTIVE_ACTIONS,
                        params={},
                        source="policy",
                    ),
                ],
                ambiguities=["unspecified_treatment_of_external_attendees"],
                intent_confidence=IntentConfidence.MEDIUM,
            )

        # 2c. Calendar Cancel / Delete Pattern (e.g. "Cancel my 10 AM meeting", "Delete evt_001", "Remove the 1on1")
        cancel_match = re.search(
            r"(?:cancel|delete|remove|drop)\s+(?:my\s+)?(?:the\s+)?(.+)",
            lower_prompt,
        )
        if cancel_match and not any(w in lower_prompt for w in ["ticket", "tkt"]):
            target_target = cancel_match.group(1).strip()
            return StructuredIntent(
                goal=f"Cancel calendar event {target_target}",
                scope="calendar",
                entities=["cancel", target_target],
                constraints=[
                    IntentConstraint(
                        type=ConstraintType.EXPECTED_STATE_EQUALS_ACTUAL_STATE,
                        params={"property": "title"},
                        source="policy",
                    ),
                ],
                ambiguities=[],
                intent_confidence=IntentConfidence.HIGH,
            )

        # 2d. Calendar Create / Schedule Pattern (flexible for all natural language phrasings)
        if any(w in lower_prompt for w in ["schedule", "book", "set up", "put on calendar"]) or (
            any(w in lower_prompt for w in ["create", "add"]) and any(w in lower_prompt for w in ["meeting", "event", "sync", "call", "session"])
        ):
            if not any(w in lower_prompt for w in ["ticket", "tkt", "bug", "issue"]):
                clk_m = re.search(r"\b(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b", lower_prompt)
                clock_part = clk_m.group(1).strip() if clk_m else "14:00"
                date_part = "today" if "today" in lower_prompt else ("tomorrow" if "tomorrow" in lower_prompt else "")

                # Clean command prefix
                clean = re.sub(
                    r"^(?:please\s+)?(?:can you\s+)?(?:schedule|book|create|set up|add|put)\s+(?:an?\s+)?(?:new\s+)?(?:event|meeting|sync|call|appointment)?(?:\.{1,4}|\s+)*",
                    "",
                    lower_prompt,
                )
                clean = re.sub(r"\b(?:for|at|from|on)\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?\b.*$", "", clean)
                clean = re.sub(r"\b(?:today|tomorrow|tonight)\b", "", clean)
                clean = re.sub(r"^[.:\s-]+|[.:\s-]+$", "", clean)
                clean = re.sub(r"^(?:called|titled)\s+", "", clean)

                if clean.startswith("with "):
                    title_part = f"Meeting {clean}".title()
                elif clean:
                    title_part = clean.title()
                else:
                    title_part = "Client Meeting" if "client" in lower_prompt else "New Meeting"

                entities = ["create", title_part, clock_part]
                if date_part:
                    entities.append(date_part)

                ambiguities = []

                return StructuredIntent(
                    goal=f"Schedule {title_part} at {clock_part}",
                    scope="calendar",
                    entities=entities,
                    constraints=[
                        IntentConstraint(
                            type=ConstraintType.NO_DESTRUCTIVE_ACTIONS,
                            params={},
                            source="policy",
                        ),
                    ],
                    ambiguities=ambiguities,
                    intent_confidence=IntentConfidence.HIGH,
                )

        # 2e. Ticket Domain Patterns (close, reopen, in_progress, create)
        if "ticket" in lower_prompt or "tkt" in lower_prompt or "bug" in lower_prompt or "issue" in lower_prompt:
            # Check for Ticket Creation
            if any(w in lower_prompt for w in ["create", "file", "open", "new", "report"]) and not re.search(r"\b(tkt[_-]?\d+|\d{3,})\b", lower_prompt):
                clean_title = re.sub(r"^(?:create|file|open|new|report)\s+(?:a\s+)?(?:new\s+)?(?:ticket|bug|issue)\s*(?:for|about|:|titled)?\s*", "", lower_prompt).strip()
                title = clean_title if clean_title else "New Bug Report"
                priority = "high" if any(w in lower_prompt for w in ["p1", "critical", "urgent", "high", "outage"]) else "medium"
                return StructuredIntent(
                    goal=f"Create ticket: {title}",
                    scope="tickets",
                    entities=["create", title, priority],
                    constraints=[
                        IntentConstraint(
                            type=ConstraintType.NO_DESTRUCTIVE_ACTIONS,
                            params={},
                            source="policy",
                        ),
                    ],
                    ambiguities=[],
                    intent_confidence=IntentConfidence.HIGH,
                )

            # Ticket Mutation on existing ticket
            tkt_match = re.search(r"(?:tkt[_-]?|ticket\s*#?\s*)(\d+)", lower_prompt)
            if tkt_match:
                ticket_id = f"tkt_{tkt_match.group(1)}"
            else:
                tkt_match2 = re.search(r"(tkt[_-]?\w+)", lower_prompt)
                ticket_id = tkt_match2.group(1).replace("-", "_") if tkt_match2 else "tkt_402"

            if "reopen" in lower_prompt:
                action = "reopen"
            elif any(w in lower_prompt for w in ["in_progress", "in progress", "start", "work on"]):
                action = "in_progress"
            elif any(w in lower_prompt for w in ["close", "resolve", "done"]):
                action = "close"
            else:
                action = "update"

            return StructuredIntent(
                goal=f"{action.capitalize()} ticket {ticket_id}",
                scope="tickets",
                entities=[ticket_id, action],
                constraints=[
                    IntentConstraint(
                        type=ConstraintType.EXPECTED_STATE_EQUALS_ACTUAL_STATE,
                        params={"property": "status"},
                        source="policy",
                    ),
                    IntentConstraint(
                        type=ConstraintType.NO_DESTRUCTIVE_ACTIONS,
                        params={},
                        source="policy",
                    ),
                ],
                ambiguities=[],
                intent_confidence=IntentConfidence.HIGH,
            )

        # Generic default
        return StructuredIntent(
            goal=normalized_prompt,
            scope="general",
            entities=[],
            constraints=[],
            ambiguities=["Unstructured prompt with unspecified entity boundaries"],
            intent_confidence=IntentConfidence.LOW,
        )
