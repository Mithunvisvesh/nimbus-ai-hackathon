from abc import ABC, abstractmethod
from typing import Optional
from src.schemas.intent import StructuredIntent


class LLMProviderError(Exception):
    """Base exception for LLM provider errors."""
    pass


class LLMResponseValidationError(LLMProviderError):
    """Raised when LLM response does not match StructuredIntent schema."""
    pass


class LLMProviderUnavailableError(LLMProviderError):
    """Raised when LLM provider is unreachable or returns network/auth error."""
    pass


class LLMProvider(ABC):
    """
    Abstract interface for LLM intent-extraction providers in the CARE Framework.
    
    The LLM's role is strictly limited to probabilistic natural language interpretation:
        Natural Language -> StructuredIntent
    
    The provider MUST NEVER:
    1. Produce or execute tool calls.
    2. Make authoritative safety, permission, or risk determinations.
    """

    @abstractmethod
    def extract_intent(self, prompt: str, user_id: Optional[str] = None) -> StructuredIntent:
        """
        Extract StructuredIntent from user natural language prompt.
        
        Args:
            prompt: Raw user input text.
            user_id: Optional user identifier.
            
        Returns:
            StructuredIntent instance matching schema.
            
        Raises:
            LLMResponseValidationError: If output is malformed or invalid schema.
            LLMProviderUnavailableError: If API call fails or times out.
            LLMProviderError: On any other provider error.
        """
        pass
