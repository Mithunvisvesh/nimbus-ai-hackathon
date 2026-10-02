import os
import json
import logging
from typing import Optional
from pydantic import ValidationError

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from google import genai
from google.genai import types
from google.genai.errors import APIError

from src.schemas.intent import StructuredIntent
from src.intent.providers.base import (
    LLMProvider,
    LLMProviderError,
    LLMResponseValidationError,
    LLMProviderUnavailableError,
)

logger = logging.getLogger(__name__)

CARE_INTENT_SYSTEM_INSTRUCTION = """
You are the Structured Intent Extractor for the CARE (Context-Aware Reasoning & Execution) Framework.
Your sole responsibility is to convert the user's natural language request into a valid StructuredIntent JSON object.

CRITICAL ARCHITECTURAL CONSTRAINTS:
1. You are ONLY an intent extractor. You do NOT have execution authority.
2. NEVER generate executable tool calls, commands, or action hashes.
3. NEVER make safety, authorization, permission, risk, or reversibility decisions. These are exclusively handled downstream by the deterministic CARE Policy Engine and Tool Metadata.

SCHEMA FIELDS:
- "goal": Normalized summary of what the user wants to achieve.
  Examples: "Move meeting from 3 pm to 4 pm", "Cancel 10 AM meeting", "Schedule sync with Alice at 4 pm", "Clear afternoon calendar", "Close ticket tkt_402", "Reopen ticket tkt_105", "Create ticket for payment gateway bug".
- "scope": Domain boundary. Must be one of: "calendar", "tickets", or "general".
- "entities": Target identifiers, times, dates, titles, or ticket IDs:
  - Calendar Reschedules: ["<source_time_or_title_or_id>", "<target_time>", "meeting", ...optional date].
  - Calendar Cancel/Delete: ["cancel", "<meeting_time_or_title_or_id>"].
  - Calendar Create: ["create", "<meeting_title>", "<start_time>", "<end_time>"].
  - Calendar Clear: ["afternoon"] or ["morning"].
  - Calendar Queries: ["query_calendar"].
  - Ticket Status Updates: ["<ticket_id>", "<status_e.g._closed_open_in_progress>"].
  - Ticket Creation: ["create", "<ticket_title>", "<priority>"].
  - Ticket Queries: ["query_tickets"].
  - General / Conversational: relevant topic keywords.
- "constraints": List of explicit or inferred constraints. Each constraint contains:
  - "type": One of "preserve_items", "max_affected", "no_destructive_actions", "expected_state_equals_actual_state".
  - "params": Key-value parameters (e.g. {"items": ["attendees", "location"]}, {"property": "start_time"}, {"property": "status"}).
  - "source": "explicit", "inferred", or "policy".
- "ambiguities": List of strings describing unresolved assumptions, vague timeframes, or unspecified treatment of sensitive items.
  - If a request is vague, broad, or lacks specifics (e.g. "Clear my afternoon so I can finish the proposal"), state the ambiguities explicitly (e.g. "unspecified_treatment_of_external_attendees", "unclear_whether_to_reschedule_or_cancel").
  - If the request is clear and explicit (e.g. "Move my 3 PM meeting to 4 PM"), ambiguities must be empty [].
- "intent_confidence": Qualitative confidence estimate: "low", "medium", or "high".
"""


class GeminiProvider(LLMProvider):
    """
    Google Gemini LLM provider for CARE intent extraction.
    Uses structured output schema enforcement via google-genai SDK.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        client: Optional[genai.Client] = None,
    ):
        self.api_key = api_key or os.getenv("GEMINI_API_KEY") or os.getenv("LLM_API_KEY")
        if not self.api_key and client is None:
            raise LLMProviderUnavailableError(
                "Gemini API key not found. Please set GEMINI_API_KEY or LLM_API_KEY in environment or .env file."
            )

        self.model = model or os.getenv("GEMINI_MODEL") or os.getenv("LLM_MODEL") or "gemini-2.5-flash"
        self.client = client or genai.Client(api_key=self.api_key)

    def extract_intent(self, prompt: str, user_id: Optional[str] = None) -> StructuredIntent:
        normalized_prompt = prompt.strip()
        if not normalized_prompt:
            raise LLMResponseValidationError("Empty prompt provided for intent extraction.")

        content_prompt = f"User Request: {normalized_prompt}"
        if user_id:
            content_prompt += f"\nRequesting Actor: {user_id}"

        try:
            config = types.GenerateContentConfig(
                system_instruction=CARE_INTENT_SYSTEM_INSTRUCTION,
                response_mime_type="application/json",
                response_schema=StructuredIntent,
                temperature=0.0,
            )
            response = self.client.models.generate_content(
                model=self.model,
                contents=content_prompt,
                config=config,
            )
        except APIError as e:
            logger.error(f"Gemini API error during intent extraction: {e}")
            raise LLMProviderUnavailableError(f"Gemini API error: {str(e)}") from e
        except Exception as e:
            logger.error(f"Gemini communication error: {e}")
            raise LLMProviderUnavailableError(f"Failed to communicate with Gemini provider: {str(e)}") from e

        if not response or not getattr(response, "text", None):
            raise LLMResponseValidationError("Gemini returned empty or null response text.")

        raw_text = response.text.strip()
        return self._parse_structured_json(raw_text)

    def _parse_structured_json(self, raw_text: str) -> StructuredIntent:
        """Sanitizes markdown wrappers and parses into typed StructuredIntent."""
        clean_text = raw_text
        if clean_text.startswith("```json"):
            clean_text = clean_text[7:]
        elif clean_text.startswith("```"):
            clean_text = clean_text[3:]
        if clean_text.endswith("```"):
            clean_text = clean_text[:-3]
        clean_text = clean_text.strip()

        try:
            data = json.loads(clean_text)
        except json.JSONDecodeError as e:
            raise LLMResponseValidationError(f"Invalid JSON returned by Gemini: {e}") from e

        if not isinstance(data, dict):
            raise LLMResponseValidationError(f"Expected JSON object from Gemini, got {type(data).__name__}")

        try:
            intent = StructuredIntent.model_validate(data)
            return intent
        except ValidationError as e:
            raise LLMResponseValidationError(f"StructuredIntent validation failed on Gemini output: {e}") from e
