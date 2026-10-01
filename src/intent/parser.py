import re
import os
import json
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any
from src.schemas.intent import (
    StructuredIntent,
    IntentConfidence,
    IntentConstraint,
    ConstraintType,
)

CACHED_RESPONSES_PATH = Path(__file__).resolve().parent.parent.parent / "demo" / "cached_llm_responses.json"

class IntentParser:
    """
    Intent parser interface for CARE.
    Extracts StructuredIntent from cached fixtures or deterministic patterns.
    A real LLM provider is teammate-owned and is not integrated here yet.
    """
    def __init__(self, use_cache: bool = True):
        self.use_cache = use_cache
        self._cached_responses: Dict[str, Any] = {}
        if self.use_cache and CACHED_RESPONSES_PATH.exists():
            try:
                with open(CACHED_RESPONSES_PATH, "r", encoding="utf-8") as f:
                    self._cached_responses = json.load(f)
            except Exception:
                self._cached_responses = {}

    def parse(self, prompt: str, user_id: Optional[str] = None) -> StructuredIntent:
        normalized_prompt = prompt.strip()

        # 1. Check offline cached response if available
        if normalized_prompt in self._cached_responses:
            return StructuredIntent.model_validate(self._cached_responses[normalized_prompt])

        # 2. Rule/Pattern fallback (ensures 100% offline hackathon robustness)
        lower_prompt = normalized_prompt.lower()

        # Calendar Reschedule Pattern (e.g. "Move 3 PM meeting to 4 PM" or "Move my 3 PM meeting to 5 PM")
        reschedule_match = re.search(
            r"move\s+(?:my\s+)?(?:the\s+)?(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\s*(?:meeting|sync)?\s+to\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)",
            lower_prompt,
        )
        if reschedule_match:
            source_time = reschedule_match.group(1).strip()
            target_time = reschedule_match.group(2).strip()
            date_match = re.search(
                r"\b(20\d{2}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/20\d{2}|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2},?\s+20\d{2})\b",
                lower_prompt,
            )
            entities = [source_time, target_time, "meeting"]
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
        if "clear" in lower_prompt and ("afternoon" in lower_prompt or "calendar" in lower_prompt):
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

        # Ticket Domain Pattern (e.g. "Close escalated ticket #402", "Close ticket #105")
        if "ticket" in lower_prompt or "tkt" in lower_prompt:
            tkt_match = re.search(r"(?:tkt[_-]?|ticket\s*#?\s*)(\d+)", lower_prompt)
            if tkt_match:
                ticket_id = f"tkt_{tkt_match.group(1)}"
            else:
                tkt_match2 = re.search(r"(tkt[_-]?\w+)", lower_prompt)
                ticket_id = tkt_match2.group(1).replace("-", "_") if tkt_match2 else "tkt_402"

            action = "close" if "close" in lower_prompt or "resolve" in lower_prompt else "update"
            return StructuredIntent(
                goal=f"{action.capitalize()} ticket {ticket_id}",
                scope="tickets",
                entities=[ticket_id],
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
